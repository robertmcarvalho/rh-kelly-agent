"""
whatsapp.py â€“ IntegraÃ§Ã£o entre o agente ADK e a API oficial do WhatsApp.

Este mÃ³dulo define utilitÃ¡rios para enviar mensagens de texto e menus interativos
(com botÃµes) via WhatsApp Business API, alÃ©m de um serviÃ§o web (FastAPI) que
recebe webhooks de mensagens de usuÃ¡rios, encaminha a entrada ao ADK e envia a
resposta apropriada de volta ao usuÃ¡rio. Ajuste as rotas e lÃ³gicas conforme sua
necessidade de negÃ³cio.

Para funcionar, defina as seguintes variÃ¡veis de ambiente no .env ou no sistema:

WHATSAPP_ACCESS_TOKEN=<seu token permanente do WhatsApp Business>
WHATSAPP_PHONE_NUMBER_ID=<ID do nÃºmero de telefone WhatsApp Business>
VERIFY_TOKEN=<uma string secreta para a verificaÃ§Ã£o do webhook>
ADK_API_URL=http://localhost:8000/apps/rh_kelly_agent  # URL do api_server do ADK
"""

import os
import base64
import requests
import json
import time
from urllib.parse import urlparse
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel
import google.generativeai as genai
from typing import List, Dict, Any, Optional
from io import BytesIO
from google.genai import types as genai_types
try:
    import redis  # type: ignore
except Exception:
    redis = None  # type: ignore
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
try:
    # Crypto helpers for Flow encryption
    from cryptography.hazmat.primitives import hashes, padding as sympadding
    from cryptography.hazmat.primitives.asymmetric import padding as asympadding, rsa
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_OK = True
except Exception:
    _CRYPTO_OK = False

# ---------------------------------------------------------------------------
# FunÃ§Ãµes utilitÃ¡rias de envio de mensagem
# ---------------------------------------------------------------------------

def _get_auth_headers() -> Dict[str, str]:
    """ObtÃ©m os cabeÃ§alhos de autenticaÃ§Ã£o para a API do WhatsApp."""
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

def _parse_first_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            return None
    try:
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(t[start:end+1])
    except Exception:
        return None
    return None

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
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        detail = getattr(response, "text", str(e))
        print(f"WhatsApp send_text_message error: {detail}")
        raise

def send_button_message(destino: str, corpo: str, botoes: List[str]) -> None:
    """
    Envia uma mensagem interativa do tipo "button".

    Args:
        destino: telefone do destinatÃ¡rio (formato internacional, ex.: '5511999999999').
        corpo: texto exibido na mensagem (pergunta).
        botoes: lista de rÃ³tulos dos botÃµes a serem exibidos. Cada botÃ£o recebe um id
                sequencial (opcao_1, opcao_2, etc.) para identificar a resposta.
    """
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    # WhatsApp limits: title 1..20 chars (no newlines). Sanitize labels.
    def _sanitize_button_title(txt: str, idx: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        if not t:
            t = f"Opcao {idx+1}"
        if len(t) > 20:
            t = t[:20]
        return t

    buttons_payload = []
    for i, label in enumerate(botoes):
        full_id = str(label)
        title = _sanitize_button_title(full_id, i)
        buttons_payload.append({
            "type": "reply",
            "reply": {"id": full_id, "title": title}
        })
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
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        detail = getattr(response, "text", str(e))
        print(f"WhatsApp send_button_message error: {detail}")
        raise

def send_list_message(destino: str, corpo: str, opcoes: List[str], botao: str = "Ver opÃ§Ãµes") -> None:
    """Envia uma mensagem interativa do tipo "list" para mais de 3 opÃ§Ãµes.

    Args:
        destino: telefone do destinatÃ¡rio (E.164, ex.: '5511999999999').
        corpo: texto exibido na mensagem.
        opcoes: lista de tÃ­tulos das linhas (cada uma vira uma row).
        botao: rÃ³tulo do botÃ£o que abre a lista.
    """
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    # WhatsApp list row title max 24 chars. Sanitize.
    def _sanitize_row_title(txt: str, idx: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        if not t:
            t = f"Opcao {idx+1}"
        if len(t) > 24:
            t = t[:24]
        return t
    rows = []
    for i, opt in enumerate(opcoes):
        full_id = str(opt)
        rows.append({
            "id": full_id,
            "title": _sanitize_row_title(full_id, i)
        })
    payload = {
        "messaging_product": "whatsapp",
        "to": destino,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": corpo},
            "action": {
                "button": botao,
                "sections": [
                    {"title": "Cidades disponÃ­veis", "rows": rows}
                ],
            },
        },
    }
    response = requests.post(url, headers=_get_auth_headers(), json=payload)
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        detail = getattr(response, "text", str(e))
        print(f"WhatsApp send_list_message error: {detail}")
        raise

def send_button_message_pairs(destino: str, corpo: str, pairs: List[Any]) -> None:
    """Envia botões com id e título separados.

    pairs: lista de tuplas (id, title) ou dicts {id,title}.
    Mantém reply.id completo e sanitiza apenas o título (≤20 chars).
    """
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    def _sanitize_title(t: str) -> str:
        s = (t or "").strip().replace("\n", " ")
        if not s:
            s = "Opcao"
        return s[:20] if len(s) > 20 else s
    buttons_payload = []
    for item in pairs:
        if isinstance(item, dict):
            _id = str(item.get("id"))
            _title = _sanitize_title(str(item.get("title")))
        else:
            _id = str(item[0])
            _title = _sanitize_title(str(item[1]))
        buttons_payload.append({"type": "reply", "reply": {"id": _id, "title": _title}})
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
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        detail = getattr(response, "text", str(e))
        print(f"WhatsApp send_button_message_pairs error: {detail}")
        raise

def send_list_message_rows(destino: str, corpo: str, rows_in: List[Any], botao: str = "Ver opcoes") -> None:
    """Envia lista com rows custom (id, title[, description]).

    rows_in aceita tuplas (id, title) ou (id, title, description),
    ou dicts {id, title} ou {id, title, description}.
    """
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    def _sanitize_row_title(txt: str, idx: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        return t[:24] if len(t) > 24 else t
    rows = []
    for idx, item in enumerate(rows_in):
        if isinstance(item, dict):
            _id = str(item.get("id"))
            _title = _sanitize_row_title(str(item.get("title")), idx)
            _desc = item.get("description")
        else:
            _id = str(item[0])
            _title = _sanitize_row_title(str(item[1]), idx)
            _desc = item[2] if len(item) > 2 else None
        row = {"id": _id, "title": _title}
        if _desc:
            row["description"] = str(_desc)
        rows.append(row)
    payload = {
        "messaging_product": "whatsapp",
        "to": destino,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": corpo},
            "action": {"button": botao, "sections": [{"rows": rows}]},
        },
    }
    response = requests.post(url, headers=_get_auth_headers(), json=payload)
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        detail = getattr(response, "text", str(e))
        print(f"WhatsApp send_list_message_rows error: {detail}")
        raise

def _extract_options_from_text(text: Optional[str]) -> List[str]:
    """HeurÃ­stica simples para extrair opÃ§Ãµes do texto do agente.

    Ex.: "Por favor, escolha uma das cidades disponÃ­veis: Apucarana ou UberlÃ¢ndia."
    -> ["Apucarana", "UberlÃ¢ndia"]
    """
    if not text:
        return []
    s = text
    # Busca trecho apÃ³s dois pontos e antes do fim/sentenÃ§a
    import re
    m = re.search(r":\s*([^\n\r]+)$", s) or re.search(r":\s*([^\.!?]+)[\.!?]", s)
    if not m:
        return []
    region = m.group(1)
    # Normaliza conectivos
    region = region.replace(" ou ", ", ")
    region = region.replace(" e ", ", ")
    # Separa por vÃ­rgulas
    parts = [p.strip() for p in region.split(",") if p.strip()]
    # Filtra partes muito curtas ou com nÃºmeros (evita ruÃ­do)
    parts = [p for p in parts if len(p) >= 2 and not any(ch.isdigit() for ch in p)]
    # Evita duplicatas preservando ordem
    seen = set()
    out: List[str] = []
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    # MantÃ©m todas; quem enviar decide se usa botÃµes (<=3) ou lista (>3)
    return out

# ---------------------------------------------------------------------------
# FunÃ§Ãµes para comunicaÃ§Ã£o com o ADK via api_server
# ---------------------------------------------------------------------------

from rh_kelly_agent.agent import root_agent
from rh_kelly_agent.agent import listar_cidades_com_vagas, verificar_vagas, SHEET_ID

# Inicializa Runner e SessionService (memÃ³ria em processo por instÃ¢ncia)
_APP_NAME = "rh_kelly_agent"
_session_service = InMemorySessionService()
_runner = Runner(app_name=_APP_NAME, agent=root_agent, session_service=_session_service)

async def enviar_mensagem_ao_agente_async(user_id: str, mensagem: str) -> Dict[str, Any]:
    """VersÃ£o assÃ­ncrona usando Runner.run_async e SessionService async."""
    sess = await _session_service.get_session(
        app_name=_APP_NAME, user_id=user_id, session_id=user_id
    )
    if not sess:
        await _session_service.create_session(
            app_name=_APP_NAME, user_id=user_id, session_id=user_id
        )

    content = genai_types.Content(parts=[genai_types.Part(text=str(mensagem or ""))])
    last_text = None
    async for event in _runner.run_async(user_id=user_id, session_id=user_id, new_message=content):
        try:
            if getattr(event, "author", "user") != "user" and getattr(event, "content", None):
                parts = getattr(event.content, "parts", None) or []
                texts = [getattr(p, "text", None) for p in parts if getattr(p, "text", None)]
                if texts:
                    last_text = "\n".join(texts).strip()
        except Exception:
            pass
    parsed = _parse_first_json(last_text or "")
    if isinstance(parsed, dict) and ("content" in parsed or "options" in parsed):
        return {
            "content": str(parsed.get("content") or ""),
            "options": parsed.get("options") if isinstance(parsed.get("options"), list) else None,
        }
    return {"content": last_text or "", "options": None}

# ---------------------------------------------------------------------------
# Suporte determinístico para funil completo (sem LLM nas etapas iniciais)
# ---------------------------------------------------------------------------

_CITIES_CACHE: Dict[str, Any] = {"expires": 0.0, "items": [], "map": {}}
_USER_CTX: Dict[str, Dict[str, Any]] = {}
_CTX_TTL_SEC = int(os.environ.get("LEAD_TTL_DAYS", "30")) * 24 * 3600

_REDIS_URL = os.environ.get("REDIS_URL")
_r = None
if _REDIS_URL and redis is not None:
    try:
        _r = redis.from_url(_REDIS_URL, decode_responses=True)
    except Exception as _rexc:
        print(f"redis init error: {_rexc}")

def _load_ctx(user_id: str) -> Dict[str, Any]:
    if _r is not None:
        try:
            raw = _r.get(f"lead_ctx:{user_id}")
            if raw:
                return json.loads(raw)
        except Exception as exc:
            print(f"redis get ctx error: {exc}")
    return _USER_CTX.get(user_id, {})

def _save_ctx(user_id: str, ctx: Dict[str, Any]) -> None:
    # always write memory map for local continuity
    _USER_CTX[user_id] = ctx
    if _r is not None:
        try:
            _r.setex(f"lead_ctx:{user_id}", _CTX_TTL_SEC, json.dumps(ctx, ensure_ascii=False))
        except Exception as exc:
            print(f"redis set ctx error: {exc}")

def _now() -> float:
    return time.time()

# Parâmetros de contingência
MODE_STRICT = (os.environ.get("AGENT_MODE", "STRICT") or "STRICT").upper()
MAX_INVALID_PER_STAGE = int(os.environ.get("MAX_INVALID_PER_STAGE", "2"))
MAX_OFF_CONTEXT = int(os.environ.get("MAX_OFF_CONTEXT", "3"))
RECAP_AFTER_MINUTES = int(os.environ.get("RECAP_AFTER_MINUTES", "30"))

def _set_last_menu(user_id: str, ctx: Dict[str, Any], *, menu_type: str, body: str, items: List[Any], botao: Optional[str] = None) -> None:
    ctx["last_menu"] = {
        "type": menu_type,  # 'buttons' | 'list'
        "body": body,
        "items": items,     # lista de pares (id,title) ou dicts {id,title}
        "button_label": botao,
    }
    _save_ctx(user_id, ctx)

def _resend_last_menu(destino: str, ctx: Dict[str, Any]) -> bool:
    lm = ctx.get("last_menu") or {}
    if not lm:
        return False
    items = lm.get("items") or []
    if lm.get("type") == "buttons":
        try:
            send_button_message_pairs(destino, lm.get("body") or "Selecione uma opção:", items)
            return True
        except Exception:
            return False
    if lm.get("type") == "list":
        try:
            send_list_message_rows(destino, lm.get("body") or "Selecione uma opção:", items, botao=(lm.get("button_label") or "Ver opções"))
            return True
        except Exception:
            return False
    return False

def _get_cities_cached(ttl_sec: int = 600) -> Dict[str, Any]:
    """Busca cidades da planilha com cache simples em memória."""
    if _CITIES_CACHE["expires"] > _now() and _CITIES_CACHE["items"]:
        return _CITIES_CACHE
    try:
        data = listar_cidades_com_vagas()
        items: List[str] = []
        if isinstance(data, dict) and data.get("status") == "success":
            items = list(map(str, data.get("cidades", []) or []))
        # mapa normalizado -> canônico
        m = {str(x).strip().lower(): str(x) for x in items}
        _CITIES_CACHE.update({
            "expires": _now() + float(ttl_sec),
            "items": items,
            "map": m,
        })
    except Exception as exc:
        print(f"cities cache error: {exc}")
        # Mantém cache anterior se houver
    return _CITIES_CACHE

def _match_city(label: str) -> Optional[str]:
    m = _get_cities_cached().get("map", {})
    return m.get(str(label or "").strip().lower())

def _send_turno_menu(destino: str, cidade: str) -> None:
    """Envia opções de turno disponíveis na cidade, de forma determinística (usado no final)."""
    try:
        res = verificar_vagas(cidade)
    except Exception as exc:
        print(f"verificar_vagas error: {exc}")
        send_text_message(destino, f"Cidade selecionada: {cidade}. Nao foi possivel consultar as vagas agora.")
        return
    if not isinstance(res, dict) or res.get("status") != "success":
        send_text_message(destino, f"Cidade selecionada: {cidade}. Nao encontrei vagas abertas no momento.")
        return
    vagas = res.get("vagas") or []
    # extrai turnos distintos e legíveis
    seen = set()
    turnos: List[str] = []
    for v in vagas:
        t = str((v or {}).get("turno", "")).strip()
        if t and t not in seen:
            seen.add(t)
            turnos.append(t)
    content = f"Cidade selecionada: {cidade}. Escolha um turno disponível:"
    try:
        print(f"city={cidade!r} turnos={turnos}")
    except Exception:
        pass
    if not turnos:
        send_text_message(destino, f"Cidade selecionada: {cidade}. Existem vagas, mas nao consegui listar turnos agora.")
        return
    if len(turnos) > 3:
        send_list_message(destino, content, turnos, botao="Ver turnos")
    else:
        send_button_message(destino, content, turnos)

def _handle_city_selection(destino: str, user_id: str, selected: str) -> Dict[str, Any]:
    cidade = _match_city(selected)
    if not cidade:
        return {"handled": False}
    ctx = {**_load_ctx(user_id)}
    ctx.update({"cidade": cidade})
    _save_ctx(user_id, ctx)
    # Após escolher a cidade, apresentar conhecimentos e perguntar se deseja continuar
    try:
        send_text_message(destino, "Ótimo! 🚀 Agora vou te explicar rapidinho sobre como funciona a nossa cooperativa.")
        send_text_message(destino, "📢 Sobre a CoopMob\nSomos uma cooperativa criada em 2023 para valorizar os entregadores 🚀. Aqui, cada motorista é dono do negócio, com voz ativa e acesso a benefícios reais.\n\n✅ Como funciona\n\nCota de participação: R$ 500 (à vista ou 25x de R$ 20, desconto em folha).\n\nPagamentos: semanais, toda quinta-feira, com base nas entregas da semana anterior.\n\nUniforme: obrigatório, camiseta custa R$ 47 (50% pago pelo cooperado, parcelado em 2x).\n\nBag: obrigatória, R$ 180 + frete (2x desconto em folha).")
        send_text_message(destino, "🎁 Benefícios para cooperados\n\nTelemedicina: gratuita 24h (dependentes pagam R$ 15).\n\nPlano odontológico: Uniodonto por R$ 16,90/mês.\n\nEducação: até 75% de desconto em cursos técnicos, graduação e pós.\n\nEnergia sustentável: até 13% de redução na conta de luz (Minas).\n\nSeguro de vida: proteção para você e sua família.")
    except Exception:
        pass
    ctx["stage"] = "ask_continue"
    _save_ctx(user_id, ctx)
    return {"handled": True}

def _send_city_menu(destino: str, user_id: str) -> None:
    """Envia saudação fixa e, em seguida, a pergunta sobre a cidade."""
    cache = _get_cities_cached()
    cities = cache.get("items", []) or []
    if not cities:
        send_text_message(destino, "No momento, não consegui obter as cidades com vagas.")
        return
    greeting = (
        "Olá, tudo bem? 😊\n"
        "Eu sou a Kelly, responsável pelo processo de recrutamento da CoopMob! 🚀\n"
        "Estou aqui para te acompanhar em cada etapa. Bora começar essa jornada juntos? 🙌"
    )
    send_text_message(destino, greeting)
    pergunta = (
        "Antes de começarmos, preciso saber: \n"
        "Em qual cidade você atua como entregador?\n"
        "Selecione no menu abaixo 👇"
    )
    pairs = [(c, c) for c in cities]
    if len(cities) > 3:
        send_list_message_rows(destino, pergunta, pairs, botao="Ver cidades")
        _set_last_menu(user_id, _load_ctx(user_id), menu_type="list", body=pergunta, items=pairs, botao="Ver cidades")
    else:
        send_button_message_pairs(destino, pergunta, pairs)
        _set_last_menu(user_id, _load_ctx(user_id), menu_type="buttons", body=pergunta, items=pairs)

def _load_conhecimento() -> List[Dict[str, str]]:
    try:
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "rh_kelly_agent", "data", "conhecimento.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception as exc:
        print(f"load conhecimento error: {exc}")
        return []

def _send_knowledge_then_continue(destino: str, user_id: Optional[str] = None) -> None:
    """Envia conteúdos de conhecimento e pergunta se deseja continuar."""
    entries = _load_conhecimento()
    if entries:
        send_text_message(destino, "Ótimo! 🚀 Agora vou te explicar rapidinho sobre como funciona a nossa cooperativa:")
        # Monta um resumo compacto (primeiros itens) para garantir entrega
        lines: List[str] = []
        char_budget = 800
        used = 0
        for e in entries:
            top = str(e.get("topico", "")).strip()
            sub = str(e.get("subtopico", "")).strip()
            cnt = str(e.get("content", "")).strip()
            header = f"• {top} • {sub}" if sub else f"• {top}"
            piece = f"{header}\n  {cnt}\n"
            if used + len(piece) > char_budget:
                break
            lines.append(piece)
            used += len(piece)
        if lines:
            send_text_message(destino, "\n".join(lines))
    # Envia resumo fixo para garantir entrega do conteúdo
    try:
        part1 = (
            "📢 Sobre a CoopMob\n"
            "Somos uma cooperativa criada em 2023 para valorizar os entregadores 🚀. "
            "Aqui, cada motorista é dono do negócio, com voz ativa e acesso a benefícios reais.\n\n"
            "✅ Como funciona\n\n"
            "Cota de participação: R$ 500 (à vista ou 25x de R$ 20, desconto em folha).\n\n"
            "Pagamentos: semanais, toda quinta-feira, com base nas entregas da semana anterior.\n\n"
            "Uniforme: obrigatório, camiseta custa R$ 47 (50% pago pelo cooperado, parcelado em 2x).\n\n"
            "Bag: obrigatória, R$ 180 + frete (2x desconto em folha)."
        )
        part2 = (
            "🎁 Benefícios para cooperados\n\n"
            "Telemedicina: gratuita 24h (dependentes pagam R$ 15).\n\n"
            "Plano odontológico: Uniodonto por R$ 16,90/mês.\n\n"
            "Educação: até 75% de desconto em cursos técnicos, graduação e pós.\n\n"
            "Energia sustentável: até 13% de redução na conta de luz (Minas).\n\n"
            "Seguro de vida: proteção para você e sua família."
        )
        send_text_message(destino, part1)
        send_text_message(destino, part2)
    except Exception as _kerr:
        print(f"knowledge send error: {_kerr}")
    # Pergunta se entendeu e deseja continuar
    body = "Você entendeu e deseja continuar com o processo seletivo?"
    pairs = [("Sim", "Sim"), ("Não", "Não")]
    send_button_message_pairs(destino, body, pairs)
    if user_id:
        _set_last_menu(user_id, _load_ctx(user_id), menu_type="buttons", body=body, items=pairs)

def _send_requirement_question(destino: str, req_key: str, user_id: Optional[str] = None) -> None:
    body = {
        "req_moto": "Você possui moto própria com documentação em dia?",
        "req_cnh": "Você possui CNH categoria A ativa?",
        "req_android": "Você possui um dispositivo Android para trabalhar?",
    }.get(req_key, "Confirma?")
    pairs = [("Sim", "Sim"), ("Não", "Não")]
    send_button_message_pairs(destino, body, pairs)
    if user_id:
        _set_last_menu(user_id, _load_ctx(user_id), menu_type="buttons", body=body, items=pairs)

def _normalize_yes_no(text: str) -> Optional[bool]:
    t = (text or "").strip().lower()
    if t in {"sim", "s", "ok", "claro", "yes"}:
        return True
    if t in {"nao", "não", "n", "no"}:
        return False
    return None

_DISC_QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": "Q1",
        "text": "Durante uma rota, você recebe uma entrega urgente. O que faz?",
        "options": [("Q1_A", "Prioriza o prazo"), ("Q1_B", "Ajusta a rota"), ("Q1_C", "Adia a entrega")],
    },
    {
        "id": "Q2",
        "text": "O cliente pede para deixar a encomenda fora do local combinado.",
        "options": [("Q2_A", "Segue a política"), ("Q2_B", "Liga para o cliente"), ("Q2_C", "Deixa assim")],
    },
    {
        "id": "Q3",
        "text": "Começa uma chuva forte durante o turno.",
        "options": [("Q3_A", "Reduz a velocidade"), ("Q3_B", "Pausa e avisa"), ("Q3_C", "Mantém o ritmo")],
    },
    {
        "id": "Q4",
        "text": "Recebe uma embalagem frágil já aberta no ponto de coleta.",
        "options": [("Q4_A", "Reporta e troca"), ("Q4_B", "Reforça e segue"), ("Q4_C", "Ignora e segue")],
    },
    {
        "id": "Q5",
        "text": "Tem duas coletas próximas com horários bem justos.",
        "options": [("Q5_A", "Confirma a ordem"), ("Q5_B", "Pede apoio"), ("Q5_C", "Arrisca atraso")],
    },
]

_DISC_SCORES = {
    "Q1_A": 1, "Q1_B": 1, "Q1_C": 0,
    "Q2_A": 1, "Q2_B": 1, "Q2_C": 0,
    "Q3_A": 1, "Q3_B": 1, "Q3_C": 0,
    "Q4_A": 1, "Q4_B": 1, "Q4_C": 0,
    "Q5_A": 1, "Q5_B": 1, "Q5_C": 0,
}

def _send_disc_question(destino: str, q_idx: int, user_id: Optional[str] = None) -> None:
    q = _DISC_QUESTIONS[q_idx]
    opts = [title for (_id, title) in q["options"]]
    body = f"Cenário: {q['text']}\nComo você agiria?"
    pairs = [(t, t) for t in opts]
    if len(opts) > 3:
        send_list_message_rows(destino, body, pairs, botao="Escolher")
        if user_id:
            _set_last_menu(user_id, _load_ctx(user_id), menu_type="list", body=body, items=pairs, botao="Escolher")
    else:
        send_button_message_pairs(destino, body, pairs)
        if user_id:
            _set_last_menu(user_id, _load_ctx(user_id), menu_type="buttons", body=body, items=pairs)

def _map_disc_selection(q_idx: int, selected_label: str) -> Optional[str]:
    q = _DISC_QUESTIONS[q_idx]
    label = (selected_label or "").strip().lower()
    for _id, title in q["options"]:
        if label == title.strip().lower():
            return _id
    return None

def _fetch_vagas_by_city(cidade: str) -> List[Dict[str, Any]]:
    try:
        res = verificar_vagas(cidade)
        if isinstance(res, dict) and res.get("status") == "success":
            return list(res.get("vagas") or [])
    except Exception as exc:
        print(f"fetch vagas error: {exc}")
    return []

def _send_vagas_menu(destino: str, cidade: str, user_id: Optional[str] = None) -> None:
    vagas = _fetch_vagas_by_city(cidade)
    if not vagas:
        send_text_message(destino, f"Aprovado! Porem, nao encontrei vagas listadas agora para {cidade}.")
        return
    # Envia detalhes completos em mensagem separada para melhor leitura
    lines = ["Vagas disponíveis:"]
    rows_labels = []  # (id, title curto)
    for v in vagas:
        vid = str(v.get("vaga_id") or v.get("VAGA_ID") or "?")
        farm = str(v.get("farmacia") or v.get("FARMACIA") or "?")
        turno = str(v.get("turno") or v.get("TURNO") or "?")
        taxa = str(v.get("taxa_entrega") or v.get("TAXA_ENTREGA") or "?")
        lines.append(f"• ID: {vid}\n  Farmácia: {farm}\n  Turno: {turno}\n  Taxa: {taxa}")
        rows_labels.append((vid, f"ID {vid} - {turno}"))
    detalhes = "\n".join(lines)
    send_text_message(destino, detalhes)
    # Em seguida, envia a lista com um corpo curto e registra menu
    body_list = "Selecione uma vaga no menu abaixo 👇"
    send_list_message_rows(destino, body_list, rows_labels, botao="Ver vagas")
    if user_id:
        _set_last_menu(user_id, _load_ctx(user_id), menu_type="list", body=body_list, items=rows_labels, botao="Ver vagas")

def _find_vaga_by_row_title(cidade: str, title_or_id: str) -> Optional[Dict[str, Any]]:
    vagas = _fetch_vagas_by_city(cidade)
    t = (title_or_id or "").strip()
    # Preferir id puro (quando usamos pairs/rows). Se vier "ID X - ...", extrai X.
    vid = t
    if t.lower().startswith("id "):
        parts = t.split(" ", 2)
        if len(parts) >= 2:
            vid = parts[1]
    for v in vagas:
        if str(v.get("vaga_id") or v.get("VAGA_ID")) == vid:
            return v
    return None

def _save_lead_record(user_id: str) -> None:
    ctx = _load_ctx(user_id) or {}
    try:
        row = {
            "user_id": user_id,
            "nome": ctx.get("nome"),
            "cidade": ctx.get("cidade"),
            "req_moto": ctx.get("req_moto"),
            "req_cnh": ctx.get("req_cnh"),
            "req_android": ctx.get("req_android"),
            "disc_score": ctx.get("disc_score"),
            "aprovado": ctx.get("aprovado"),
            "vaga_id": (ctx.get("vaga") or {}).get("VAGA_ID") or (ctx.get("vaga") or {}).get("vaga_id"),
            "turno": (ctx.get("vaga") or {}).get("TURNO") or (ctx.get("vaga") or {}).get("turno"),
            "farmacia": (ctx.get("vaga") or {}).get("FARMACIA") or (ctx.get("vaga") or {}).get("farmacia"),
            "taxa_entrega": (ctx.get("vaga") or {}).get("TAXA_ENTREGA") or (ctx.get("vaga") or {}).get("taxa_entrega"),
            "timestamp": int(time.time()),
        }
        creds_json = os.environ.get("GSHEETS_SERVICE_ACCOUNT_JSON")
        if creds_json:
            import gspread
            import json as _json
            sa = gspread.service_account_from_dict(_json.loads(creds_json))
            sh = sa.open_by_key(SHEET_ID)
            ws_title = os.environ.get("LEADS_SHEET_TITLE", "Leads")
            ws = sh.worksheet(ws_title)
            # Reordena segundo o cabecalho existente, quando disponível
            try:
                header = ws.row_values(1)
                from datetime import datetime, timezone
                iso = datetime.now(timezone.utc).isoformat()
                aprovado = row.get("aprovado")
                score = row.get("disc_score")
                protocolo = f"{int(time.time())}-{user_id}"
                turno = row.get("turno")
                mapping = {
                    "DATA_ISO": iso,
                    "NOME": row.get("nome"),
                    "TELEFONE": row.get("user_id"),
                    "PERFIL_APROVADO": "Sim" if aprovado else "Não",
                    "PERFIL_NOTA": score,
                    "PROTOCOLO": protocolo,
                    "TURNO_ESCOLHIDO": turno,
                    "VAGA_ID": row.get("vaga_id"),
                    "FARMACIA": row.get("farmacia"),
                    "CIDADE": row.get("cidade"),
                    "TAXA_ENTREGA": row.get("taxa_entrega"),
                }
                values = [mapping.get(h, row.get(h)) for h in header]
                ws.append_row(values, value_input_option="USER_ENTERED")
            except Exception as ws_exc:
                print(f"sheets append error: {ws_exc}")
        # Sempre guarda no Redis (quando disponível)
        if _r is not None:
            try:
                _r.rpush("leads_records", json.dumps(row, ensure_ascii=False))
                _r.set(f"lead_final:{user_id}", json.dumps(row, ensure_ascii=False))
            except Exception as rex:
                print(f"redis save lead error: {rex}")
    except Exception as exc:
        print(f"save lead error: {exc}")

# ---------------------------------------------------------------------------
# Áudio: download do WhatsApp e transcrição via Gemini
# ---------------------------------------------------------------------------

def _download_whatsapp_media(media_id: str) -> Optional[Dict[str, Any]]:
    try:
        token = os.environ["WHATSAPP_ACCESS_TOKEN"]
        # 1) Obter URL
        meta = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        meta.raise_for_status()
        j = meta.json()
        url = j.get("url")
        mime = j.get("mime_type") or j.get("mime")
        if not url:
            return None
        # 2) Baixar binário
        binr = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        binr.raise_for_status()
        return {"bytes": binr.content, "mime_type": mime or "audio/ogg"}
    except Exception as exc:
        print(f"download media error: {exc}")
        return None

def _transcribe_audio_gemini(data: bytes, mime_type: str) -> Optional[str]:
    try:
        model_name = os.environ.get("AUDIO_TRANSCRIBE_MODEL") or os.environ.get("AGENT_MODEL") or "gemini-2.5-flash"
        model = genai.GenerativeModel(model_name)
        parts = [
            {"mime_type": mime_type or "audio/ogg", "data": data},
            {"text": "Transcreva o áudio em português do Brasil. Responda apenas com a transcrição, sem comentários."},
        ]
        resp = model.generate_content(parts)
        return getattr(resp, "text", None)
    except Exception as exc:
        print(f"audio transcribe error: {exc}")
        return None


def processar_resposta_do_agente(destino: str, resposta: Dict[str, Any]) -> None:
    """
    Processa a resposta do ADK e envia ao usuÃ¡rio via WhatsApp.

    A resposta do ADK pode incluir:
      - 'content': texto simples a ser enviado.
      - 'function_call': chamada de funÃ§Ã£o (caso o agente indique que deve chamar uma ferramenta).
      - 'options': lista de strings sugeridas como botÃµes (customizaÃ§Ã£o prÃ³pria).
    Esta funÃ§Ã£o interpreta esses campos e decide se envia texto ou menu.
    Ajuste conforme o formato das respostas do seu agente.
    """
    # Exemplo simplificado:
    content = resposta.get("content")
    options = resposta.get("options")  # Ex.: lista de strings para botÃµes
    # HeurÃ­stica: se nÃ£o veio options mas o texto aparenta listar escolhas, gera botÃµes
    if not options:
        inferred = _extract_options_from_text(content or "")
        if len(inferred) >= 2:
            options = inferred

    if options:
        if len(options) > 3:
            send_list_message(destino, content or "Selecione uma opÃ§Ã£o:", options)
        else:
            send_button_message(destino, content or "Selecione uma opÃ§Ã£o:", options)
    else:
        send_text_message(destino, content or "Desculpe, nÃ£o consegui entender.")

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
    Endpoint para a verificaÃ§Ã£o do webhook do WhatsApp (conforme documentaÃ§Ã£o da Meta).
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

    - Extrai o nÃºmero do remetente e a mensagem ou ID do botÃ£o.
    - Envia a entrada ao agente ADK.
    - Processa a resposta do agente e envia de volta via WhatsApp.

    Este exemplo suporta mensagens de texto e cliques em botÃµes (reply.id).
    """
    data = await request.json()
    try:
        # A estrutura real do webhook pode variar; ajuste conforme a configuraÃ§Ã£o do Meta.
        entry = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        messages = entry.get("messages")
        if not messages:
            return {"status": "ignored"}
        msg = messages[0]
        from_number = msg.get("from", "")  # telefone do usuario
        # Captura e persiste o nome do lead (quando disponível)
        try:
            contacts = entry.get("contacts") or []
            profile_name = None
            if contacts:
                profile_name = ((contacts[0] or {}).get("profile") or {}).get("name")
            if profile_name:
                _ctx_tmp = _load_ctx(from_number) or {}
                if not _ctx_tmp.get("nome"):
                    _ctx_tmp["nome"] = str(profile_name)
                    _save_ctx(from_number, _ctx_tmp)
        except Exception:
            pass

        # Nao injeta saudacao/menu aqui: o ADK conduz o fluxo inicial
        # Extrai texto do usuario apenas para tipos suportados
        texto_usuario = ""
        was_audio = False
        try:
            mtype = msg.get("type")
            if mtype == "text":
                texto_usuario = msg.get("text", {}).get("body", "")
            elif mtype == "interactive":
                interactive = msg.get("interactive", {})
                itype = interactive.get("type")
                if itype == "button_reply":
                    br = interactive.get("button_reply", {})
                    bid = br.get("id")
                    btitle = br.get("title", "")
                    texto_usuario = bid or btitle
                    try:
                        print(f"interactive button_reply id={bid!r} title={btitle!r}")
                    except Exception:
                        pass
                elif itype == "list_reply":
                    lr = interactive.get("list_reply", {})
                    lid = lr.get("id")
                    ltitle = lr.get("title", "")
                    texto_usuario = lid or ltitle
                    try:
                        print(f"interactive list_reply id={lid!r} title={ltitle!r}")
                    except Exception:
                        pass
            elif mtype == "audio":
                media = msg.get("audio", {}) or {}
                mid = media.get("id")
                if mid:
                    try:
                        mdat = _download_whatsapp_media(mid)
                        if mdat and mdat.get("bytes"):
                            texto_usuario = _transcribe_audio_gemini(mdat["bytes"], mdat.get("mime_type") or "audio/ogg") or ""
                            print(f"audio transcribed chars={len(texto_usuario or '')}")
                            was_audio = True
                    except Exception as aexc:
                        print(f"audio handle error: {aexc}")
        except Exception:
            texto_usuario = ""

        # Ignora mensagens sem conteudo textual interpretavel
        if not (texto_usuario or "").strip():
            if was_audio:
                try:
                    send_text_message(from_number, "Não consegui entender seu áudio. Pode escrever a mensagem?")
                except Exception:
                    pass
            return {"status": "ignored"}

        # Fluxo determinístico por estagio
        ctx = _load_ctx(from_number) or {}
        stage = ctx.get("stage")
        if not stage:
            ctx["stage"] = "await_city"
            ctx["invalid_count"] = 0
            ctx["off_context_count"] = 0
            ctx["last_message_at"] = _now()
            _save_ctx(from_number, ctx)
            _send_city_menu(from_number, from_number)
            return {"status": "handled"}

        # Se já finalizado, não reiniciar automaticamente
        if stage == "final":
            send_text_message(from_number, "Atendimento finalizado. Para recomeçar, digite 'recomeçar'.")
            return {"status": "handled"}

        # Recapitular após inatividade
        try:
            last_ts = float(ctx.get("last_message_at") or 0)
            if _now() - last_ts > RECAP_AFTER_MINUTES * 60 and ctx.get("last_menu"):
                send_text_message(from_number, "Retomando de onde paramos. Aqui estão as opções novamente 👇")
                if _resend_last_menu(from_number, ctx):
                    ctx["last_message_at"] = _now()
                    _save_ctx(from_number, ctx)
                    return {"status": "handled"}
        except Exception:
            pass

        # Comandos globais
        def _cmd(txt: str) -> str:
            t = (txt or "").strip().lower()
            if t in {"menu"}: return "menu"
            if t in {"voltar"}: return "voltar"
            if t in {"recomecar", "recomeçar"}: return "recomecar"
            if t in {"ajuda", "help"}: return "ajuda"
            if t in {"humano", "atendente", "suporte"}: return "humano"
            if t in {"status", "progresso"}: return "status"
            return ""

        cmd = _cmd(texto_usuario)
        if cmd == "recomecar":
            ctx = {"stage": "await_city", "invalid_count": 0, "off_context_count": 0, "last_message_at": _now()}
            _save_ctx(from_number, ctx)
            _send_city_menu(from_number, from_number)
            return {"status": "handled"}
        if cmd == "menu" and ctx.get("last_menu"):
            send_text_message(from_number, "Claro! Aqui estão as opções novamente 👇")
            _resend_last_menu(from_number, ctx)
            ctx["last_message_at"] = _now()
            _save_ctx(from_number, ctx)
            return {"status": "handled"}
        if cmd == "voltar":
            st = str(ctx.get("stage") or "")
            def back_to(prev_stage: str):
                ctx["stage"] = prev_stage
                ctx["invalid_count"] = 0
                _save_ctx(from_number, ctx)
            if st.startswith("disc_q"):
                try:
                    qi = int(st.replace("disc_q", ""))
                except Exception:
                    qi = 0
                if qi > 0:
                    back_to(f"disc_q{qi-1}")
                    _send_disc_question(from_number, qi-1, user_id=from_number)
                else:
                    back_to("req_android")
                    _send_requirement_question(from_number, "req_android", user_id=from_number)
                return {"status": "handled"}
            if st == "offer_positions":
                # Reenviar vagas
                _resend_last_menu(from_number, ctx) or _send_vagas_menu(from_number, ctx.get("cidade") or "", user_id=from_number)
                return {"status": "handled"}
            if st == "req_android":
                back_to("req_cnh"); _send_requirement_question(from_number, "req_cnh", user_id=from_number); return {"status": "handled"}
            if st == "req_cnh":
                back_to("req_moto"); _send_requirement_question(from_number, "req_moto", user_id=from_number); return {"status": "handled"}
            if st == "req_moto":
                back_to("ask_continue"); send_button_message_pairs(from_number, "Você entendeu e deseja continuar com o processo seletivo?", [("Sim","Sim"),("Não","Não")]); _set_last_menu(from_number, _load_ctx(from_number), menu_type="buttons", body="Você entendeu e deseja continuar com o processo seletivo?", items=[("Sim","Sim"),("Não","Não")]); return {"status": "handled"}
            if st == "ask_continue":
                back_to("await_city"); _send_city_menu(from_number, from_number); return {"status": "handled"}
            # default: reenviar ultimo menu
            if _resend_last_menu(from_number, ctx):
                return {"status": "handled"}
        if cmd == "ajuda":
            st = str(ctx.get("stage") or "")
            tips = {
                "await_city": "Toque em uma das cidades do menu para continuar.",
                "ask_continue": "Toque em Sim ou Não para escolher seguir com o processo.",
                "req_moto": "Responda tocando em Sim ou Não.",
                "req_cnh": "Responda tocando em Sim ou Não.",
                "req_android": "Responda tocando em Sim ou Não.",
                "offer_positions": "Toque em uma vaga do menu para selecionar.",
            }
            send_text_message(from_number, f"Ajuda: {tips.get(st, 'Selecione uma opção do menu abaixo.')} ")
            _resend_last_menu(from_number, ctx)
            return {"status": "handled"}
        if cmd == "status":
            st_map = {
                "await_city": "Aguardando seleção de cidade",
                "ask_continue": "Apresentação enviada; aguardando confirmação para continuar",
                "req_moto": "Confirmando: moto com documentação em dia",
                "req_cnh": "Confirmando: CNH A ativa",
                "req_android": "Confirmando: dispositivo Android",
                "disc_q0": "Questionário DISC (1/5)",
                "disc_q1": "Questionário DISC (2/5)",
                "disc_q2": "Questionário DISC (3/5)",
                "disc_q3": "Questionário DISC (4/5)",
                "disc_q4": "Questionário DISC (5/5)",
                "offer_positions": "Apresentando vagas disponíveis",
                "final": "Atendimento concluído",
            }
            nome = ctx.get("nome") or "Entregador(a)"
            cidade = ctx.get("cidade") or "—"
            reqs = [
                f"Moto: {'Sim' if ctx.get('req_moto') else 'Não' if ctx.get('req_moto') is False else '—'}",
                f"CNH A: {'Sim' if ctx.get('req_cnh') else 'Não' if ctx.get('req_cnh') is False else '—'}",
                f"Android: {'Sim' if ctx.get('req_android') else 'Não' if ctx.get('req_android') is False else '—'}",
            ]
            disc_prog = ctx.get("disc_answers") or []
            vaga = ctx.get("vaga") or {}
            msg = (
                f"Status de {nome}:\n"
                f"• Etapa: {st_map.get(str(ctx.get('stage') or ''), '—')}\n"
                f"• Cidade: {cidade}\n"
                f"• Requisitos: {', '.join(reqs)}\n"
                f"• DISC: {len(disc_prog)}/5 respondidas\n"
            )
            if vaga:
                msg += f"• Vaga: ID {vaga.get('VAGA_ID') or vaga.get('vaga_id')} ({vaga.get('TURNO') or vaga.get('turno')})\n"
            msg += "\nDicas: digite 'menu' para ver as opções, 'voltar' para a etapa anterior ou 'recomeçar' para iniciar do zero."
            send_text_message(from_number, msg)
            if ctx.get("last_menu"):
                _resend_last_menu(from_number, ctx)
            return {"status": "handled"}
        if cmd == "humano":
            send_text_message(from_number, "Sem problemas! Vou pedir para nossa equipe te chamar. Você também pode preencher o formulário: https://app.pipefy.com/public/form/v2m7kpB-")
            ctx["stage"] = "final"; _save_ctx(from_number, ctx)
            return {"status": "handled"}

        # Trata selecao de cidade localmente (sem LLM), quando aplicavel
        try:
            handled = _handle_city_selection(from_number, from_number, texto_usuario)
            if handled.get("handled"):
                try:
                    print(f"city matched OK -> {handled}")
                except Exception:
                    pass
                return {"status": "handled"}
        except Exception as sel_exc:
            print(f"city selection handler error: {sel_exc}")

        # Estagios sequenciais
        try:
            if stage == "ask_continue":
                yn = _normalize_yes_no(texto_usuario)
                if yn is True:
                    # Mensagem de contexto antes dos requisitos
                    send_text_message(from_number, "Perfeito! Antes de seguir, preciso confirmar alguns requisitos rápidos.")
                    ctx["stage"] = "req_moto"
                    _save_ctx(from_number, ctx)
                    _send_requirement_question(from_number, "req_moto", user_id=from_number)
                    return {"status": "handled"}
                if yn is False:
                    send_text_message(from_number, "Tudo bem. Fico à disposição para futuras oportunidades. Obrigada!")
                    ctx["stage"] = "final"
                    _save_ctx(from_number, ctx)
                    return {"status": "handled"}

            if stage == "req_moto":
                yn = _normalize_yes_no(texto_usuario)
                if yn is not None:
                    ctx["req_moto"] = bool(yn)
                    ctx["stage"] = "req_cnh"
                    _save_ctx(from_number, ctx)
                    send_text_message(from_number, "Ótimo, obrigada pela confirmação.")
                    _send_requirement_question(from_number, "req_cnh", user_id=from_number)
                    return {"status": "handled"}

            if stage == "req_cnh":
                yn = _normalize_yes_no(texto_usuario)
                if yn is not None:
                    ctx["req_cnh"] = bool(yn)
                    ctx["stage"] = "req_android"
                    _save_ctx(from_number, ctx)
                    send_text_message(from_number, "Perfeito, mais uma pergunta rápida.")
                    _send_requirement_question(from_number, "req_android", user_id=from_number)
                    return {"status": "handled"}

            if stage == "req_android":
                yn = _normalize_yes_no(texto_usuario)
                if yn is not None:
                    ctx["req_android"] = bool(yn)
                    # Checa requisitos
                    if ctx.get("req_moto") and ctx.get("req_cnh") and ctx.get("req_android"):
                        ctx["stage"] = "disc_q0"
                        ctx["disc_answers"] = []
                        _save_ctx(from_number, ctx)
                        send_text_message(from_number, "Excelente! Agora vou fazer 5 perguntas rápidas para entender seu perfil.")
                        _send_disc_question(from_number, 0, user_id=from_number)
                    else:
                        send_text_message(from_number, "Obrigada pelo interesse. No momento, os requisitos necessários não foram atendidos.")
                        ctx["stage"] = "final"
                        _save_ctx(from_number, ctx)
                    return {"status": "handled"}

            if stage and stage.startswith("disc_q"):
                try:
                    q_idx = int(stage.replace("disc_q", ""))
                except Exception:
                    q_idx = 0
                ans_id = _map_disc_selection(q_idx, texto_usuario)
                if ans_id:
                    answers = ctx.get("disc_answers") or []
                    answers.append(ans_id)
                    ctx["disc_answers"] = answers
                    if q_idx + 1 < len(_DISC_QUESTIONS):
                        ctx["stage"] = f"disc_q{q_idx+1}"
                        _save_ctx(from_number, ctx)
                        _send_disc_question(from_number, q_idx+1)
                    else:
                        # Avalia
                        score = sum(_DISC_SCORES.get(a, 0) for a in answers)
                        ctx["disc_score"] = score
                        aprovado = score >= 3
                        ctx["aprovado"] = aprovado
                        _save_ctx(from_number, ctx)
                        if aprovado:
                            send_text_message(from_number, "Parabéns! Você foi aprovado(a).")
                            _send_vagas_menu(from_number, ctx.get("cidade") or "")
                            ctx["stage"] = "offer_positions"
                            _save_ctx(from_number, ctx)
                        else:
                            send_text_message(from_number, "Obrigado por participar. Neste momento, não seguiremos adiante.")
                            ctx["stage"] = "final"
                            _save_ctx(from_number, ctx)
                    return {"status": "handled"}

            if stage == "offer_positions":
                cidade = ctx.get("cidade") or ""
                vaga = _find_vaga_by_row_title(cidade, texto_usuario)
                if vaga:
                    ctx["vaga"] = {
                        "VAGA_ID": vaga.get("VAGA_ID") or vaga.get("vaga_id"),
                        "FARMACIA": vaga.get("FARMACIA") or vaga.get("farmacia"),
                        "TURNO": vaga.get("TURNO") or vaga.get("turno"),
                        "TAXA_ENTREGA": vaga.get("TAXA_ENTREGA") or vaga.get("taxa_entrega"),
                    }
                    _save_ctx(from_number, ctx)
                    # Link do Pipefy fixo informado
                    link_url = "https://app.pipefy.com/public/form/v2m7kpB-"
                    # Reapresenta detalhes completos antes do link
                    det_vid = ctx["vaga"].get("VAGA_ID")
                    det_farm = ctx["vaga"].get("FARMACIA")
                    det_turno = ctx["vaga"].get("TURNO")
                    det_taxa = ctx["vaga"].get("TAXA_ENTREGA")
                    send_text_message(from_number, (
                        f"Vaga selecionada:\n"
                        f"• ID: {det_vid}\n• Farmácia: {det_farm}\n• Turno: {det_turno}\n• Taxa: {det_taxa}"
                    ))
                    send_text_message(from_number, (
                        f"Ótimo! Para concluir sua matrícula, preencha o formulário: {link_url}.\n"
                        "Nossa equipe entrará em contato em até 48 horas. Obrigada!"
                    ))
                    _save_lead_record(from_number)
                    ctx["stage"] = "final"
                    _save_ctx(from_number, ctx)
                    return {"status": "handled"}
        except Exception as flow_exc:
            print(f"flow error: {flow_exc}")

        # Modo estrito: se há um stage ativo e a entrada não foi tratada acima,
        # reenvia o último menu em vez de chamar LLM.
        try:
            ctx = _load_ctx(from_number) or {}
            st = ctx.get("stage")
            if MODE_STRICT == "STRICT" and st not in (None, "final"):
                if not _resend_last_menu(from_number, ctx):
                    _send_city_menu(from_number, from_number)
                ctx["invalid_count"] = int(ctx.get("invalid_count") or 0) + 1
                ctx["off_context_count"] = int(ctx.get("off_context_count") or 0) + 1
                ctx["last_message_at"] = _now()
                _save_ctx(from_number, ctx)
                return {"status": "handled"}
        except Exception:
            pass

        # Se nao ha cidade no contexto, envia saudacao fixa + menu de cidades
        try:
            ctx = _USER_CTX.get(from_number) or {}
            if not ctx.get("cidade"):
                matched = _match_city(texto_usuario)
                if matched:
                    handled = _handle_city_selection(from_number, from_number, matched)
                    if handled.get("handled"):
                        return {"status": "handled"}
                _send_city_menu(from_number)
                return {"status": "handled"}
        except Exception as greet_exc:
            print(f"greeting/menu error: {greet_exc}")
        # Encaminha a entrada ao agente com tratamento de falhas (async)
        try:
            agent_response = await enviar_mensagem_ao_agente_async(from_number, texto_usuario)
            # Processa e envia de volta via WhatsApp
            processar_resposta_do_agente(from_number, agent_response)
        except Exception as inner_exc:
            print(f"Agent pipeline error: {inner_exc}")
            # Envia fallback simples para o usuÃ¡rio e segue com 200
            try:
                send_text_message(
                    from_number,
                    "NÃ£o consegui processar sua mensagem agora. Tente novamente em instantes.",
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
    """Retorna o status das variÃ¡veis de ambiente crÃ­ticas (sem expor segredos)."""
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


@app.get("/llm-ping")
def llm_ping():
    """Executa uma chamada mÃ­nima ao modelo Gemini para verificar conectividade."""
    try:
        model_name = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")
        model = genai.GenerativeModel(model_name)
        resp = model.generate_content("ping")
        # Extrai texto quando disponÃ­vel
        out = getattr(resp, "text", None)
        return {
            "status": "ok",
            "model": model_name,
            "has_text": bool(out),
            "text": out,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


class SendButtonsRequest(BaseModel):
    to: str
    body: str
    buttons: List[str]


@app.post("/send-buttons")
def send_buttons_endpoint(payload: SendButtonsRequest, authorization: Optional[str] = Header(default=None)):
    """Endpoint para disparar mensagem com botÃµes (mÃ¡x 3) para testes.

    Se INTERNAL_API_TOKEN estiver definida, exige Authorization: Bearer <token>.
    """
    required_token = os.environ.get("INTERNAL_API_TOKEN")
    if required_token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization.split(" ", 1)[1]
        if token != required_token:
            raise HTTPException(status_code=403, detail="Invalid token")

    btns = [b.strip() for b in (payload.buttons or []) if isinstance(b, str) and b.strip()]
    if not btns:
        raise HTTPException(status_code=400, detail="buttons must be a non-empty list of labels")
    if len(btns) > 3:
        btns = btns[:3]
    try:
        send_button_message(payload.to, payload.body, btns)
        return {"status": "sent", "buttons": btns}
    except requests.HTTPError as http_err:
        status = getattr(http_err.response, "status_code", 500)
        detail = getattr(http_err.response, "text", str(http_err))
        raise HTTPException(status_code=status, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/agent-ping")
def agent_ping(user_id: Optional[str] = None, text: Optional[str] = None):
    """Passa uma mensagem simples ao agente via Runner e retorna o texto final."""
    uid = user_id or "diagnostic-user"
    msg = text or "ping"
    try:
        # garante sessÃ£o
        try:
            _ = _session_service.get_session_sync(app_name=_APP_NAME, user_id=uid, session_id=uid)
        except Exception:
            _ = None
        if not _:
            _session_service.create_session_sync(app_name=_APP_NAME, user_id=uid, session_id=uid)

        content = genai_types.Content(parts=[genai_types.Part(text=msg)])
        last_text = None
def _b64_decode(data: str) -> bytes:
    """Decode Base64 (standard or urlsafe) handling missing padding.

    Returns the decoded bytes or raises ValueError if decoding fails.
    """
    if not data:
        return b""
    s = data.strip()
    padding = "=" * (-len(s) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(s + padding)
        except Exception:
            continue
    raise ValueError("invalid_base64")


        count = 0
        for event in _runner.run(user_id=uid, session_id=uid, new_message=content):
            count += 1
            try:
                if getattr(event, "author", "user") != "user" and getattr(event, "content", None):
                    parts = getattr(event.content, "parts", None) or []
                    texts = [getattr(p, "text", None) for p in parts if getattr(p, "text", None)]
                    if texts:
                        last_text = "\n".join(texts).strip()
            except Exception:
                pass
        return {"status": "ok", "events": count, "text": last_text}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

# ---------------------------------------------------------------------------
# WhatsApp Flows Data API endpoint (integrity-check friendly)
# ---------------------------------------------------------------------------

def _b64_encode_json(obj: Dict[str, Any]) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        # Encodes {"status":"ok"} as fallback
        return "eyJzdGF0dXMiOiJvayJ9"


@app.get("/flow-data", response_class=PlainTextResponse)
def flow_data_get():
    """Minimal GET that returns a Base64-encoded JSON body."""
    return PlainTextResponse(content=_b64_encode_json({"status": "ok"}), media_type="text/plain")


def _load_flow_private_key() -> Optional[object]:
    if not _CRYPTO_OK:
        return None
    p = os.environ.get("FLOW_PRIVATE_KEY_PATH", os.path.join(os.getcwd(), "secrets", "flow_private.pem"))
    try:
        with open(p, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except Exception as exc:
        try:
            print(f"flow private key load error: {exc}")
        except Exception:
            pass
        return None


def _rsa_oaep_decrypt(priv_key: object, data: bytes) -> Optional[bytes]:
    try:
        return priv_key.decrypt(
            data,
            asympadding.OAEP(mgf=asympadding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    except Exception:
        return None


def _aescbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> Optional[bytes]:
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        dec = cipher.decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpadder = sympadding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception:
        return None


def _aescbc_encrypt(key: bytes, iv: bytes, pt: bytes) -> bytes:
    padder = sympadding.PKCS7(128).padder()
    padded = padder.update(pt) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _aesgcm_decrypt(key: bytes, iv: bytes, data: bytes) -> Optional[bytes]:
    try:
        # Encrypted payload handling
                enc_key_b = _b64_decode(parsed.get("encrypted_aes_key") or "")
                iv_b = _b64_decode(parsed.get("initial_vector") or "")
                ct_b = _b64_decode(parsed.get("encrypted_flow_data") or "")

            if pt is None and len(iv_b) == 16 and (len(ct_b) % 16 == 0):
                pt = _aescbc_decrypt(aes_key, iv_b, ct_b)
                if pt is not None:
                    mode = "CBC"


            def _invert_bytes(data: bytes) -> bytes:
                return bytes(b ^ 0xFF for b in data)
            if mode == "GCM":
                resp_iv = _invert_bytes(iv_b)
                ct_out = _aesgcm_encrypt(aes_key, resp_iv, pt_resp)
            elif mode == "CBC":
                resp_iv = _invert_bytes(iv_b)
                ct_out = _aescbc_encrypt(aes_key, resp_iv, pt_resp)
            else:
                return PlainTextResponse(content=_b64_encode_json({"status": "unsupported_cipher"}), media_type="text/plain")

            return PlainTextResponse(content=base64.b64encode(ct_out).decode("ascii"), media_type="text/plain")
        # If encrypted payload present, perform decrypt -> build response -> encrypt
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            parsed = {}

        if _CRYPTO_OK and isinstance(parsed, dict) and {
            "encrypted_flow_data",
            "encrypted_aes_key",
            "initial_vector",
        }.issubset(parsed.keys()):
            try:
                priv = _load_flow_private_key()
                if not priv:
                    raise RuntimeError("private_key_unavailable")
                enc_key_b = base64.b64decode(parsed.get("encrypted_aes_key") or "")
                iv_b = base64.b64decode(parsed.get("initial_vector") or "")
                ct_b = base64.b64decode(parsed.get("encrypted_flow_data") or "")
            except Exception as e:
                print(f"flow-data decode error: {e}")
                return PlainTextResponse(content=_b64_encode_json({"status": "decode_error"}), media_type="text/plain")

            # Decrypt AES key with RSA OAEP SHA-256
            aes_key = _rsa_oaep_decrypt(priv, enc_key_b) if priv else None
            if not aes_key:
                return PlainTextResponse(content=_b64_encode_json({"status": "key_error"}), media_type="text/plain")

            # Try AES-GCM first, then AES-CBC
            pt = None
            mode = None
            if len(iv_b) in (12, 16):
                pt = _aesgcm_decrypt(aes_key, iv_b, ct_b)
                if pt is not None:
                    mode = "GCM"
            if pt is None:
                # CBC requires IV length 16 and ciphertext multiple of 16
                if len(iv_b) == 16 and (len(ct_b) % 16 == 0):
                    pt = _aescbc_decrypt(aes_key, iv_b, ct_b)
                    if pt is not None:
                        mode = "CBC"

            # Build a minimal OK response payload (could echo action)
            try:
                incoming = json.loads(pt.decode("utf-8")) if pt else {}
            except Exception:
                incoming = {}
            response_obj = {"status": "ok"}
            if isinstance(incoming, dict) and incoming:
                response_obj["echo"] = incoming.get("action") or incoming

            pt_resp = json.dumps(response_obj, ensure_ascii=False).encode("utf-8")

            # Enc        # Encrypt response using inverted IV and same AES key
        def _invert_bytes(data: bytes) -> bytes:
            return bytes([(b ^ 0xFF) for b in data])

        if mode == "GCM":
            resp_iv = _invert_bytes(iv_b)
            ct_out = _aesgcm_encrypt(aes_key, resp_iv, pt_resp)
        elif mode == "CBC":
            resp_iv = _invert_bytes(iv_b)
            ct_out = _aescbc_encrypt(aes_key, resp_iv, pt_resp)
        else:
            return PlainTextResponse(content=_b64_encode_json({"status": "unsupported_cipher"}), media_type="text/plain")

        return PlainTextResponse(content=base64.b64encode(ct_out).decode("ascii"), media_type="text/plain")
        # Non-encrypted mode: return Base64-encoded minimal body
        reply: Dict[str, Any] = {"status": "ok"}
        if isinstance(parsed, dict) and parsed:
            reply["echo"] = parsed.get("action") or parsed
        return PlainTextResponse(content=_b64_encode_json(reply), media_type="text/plain")
    except Exception as exc:
        try:
            print(f"flow-data error: {exc}")
        except Exception:
            pass
        # Return a valid Base64 string even on error
        return PlainTextResponse(content="eyJzdGF0dXMiOiJva19lcnJvciJ9", media_type="text/plain")


# Provide common alias path as well
app.add_api_route("/flows/data", flow_data_post, methods=["POST"], response_class=PlainTextResponse)
app.add_api_route("/flows/data", flow_data_get, methods=["GET"], response_class=PlainTextResponse)


# Debug helper to inspect last Flow request
_FLOW_LAST: Dict[str, Any] = {"headers": {}, "body": "", "ts": None}


@app.get("/flow-debug")
def flow_debug():
    try:
        return JSONResponse(_FLOW_LAST)
    except Exception:
        return JSONResponse({"headers": {}, "body": None, "ts": None})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)



