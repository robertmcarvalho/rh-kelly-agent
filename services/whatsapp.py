"""
Integração entre o agente ADK e a API oficial do WhatsApp.

Este módulo define utilitários para enviar mensagens de texto e menus interativos
(com botões) via WhatsApp Business API, além de um serviço web (FastAPI) que
recebe webhooks de mensagens de usuários, encaminha a entrada ao ADK e envia a
resposta apropriada de volta ao usuário. Ajuste as rotas e lógicas conforme sua
necessidade de negócio.

Para funcionar, defina as seguintes variáveis de ambiente no .env ou no sistema:

- WHATSAPP_ACCESS_TOKEN: <seu token permanente do WhatsApp Business>
- WHATSAPP_PHONE_NUMBER_ID: <ID do número de telefone WhatsApp Business>
- VERIFY_TOKEN: <uma string secreta para a verificação do webhook>
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
    import redis
except ImportError:
    redis = None

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from contextlib import asynccontextmanager
from rh_kelly_agent.agent import root_agent
from rh_kelly_agent.config import AGENT_MODEL
from rh_kelly_agent.agent import listar_cidades_com_vagas, verificar_vagas, SHEET_ID

# Helpers
def _env_true(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

def _strip_accents(text: str) -> str:
    """Remove acentos para facilitar a normalização de entradas (ex.: 'não' -> 'nao')."""
    try:
        import unicodedata
        t = str(text or "")
        return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    except Exception:
        return str(text or "")

# Intro script loading
_INTRO_SCRIPT: Dict[str, Any] = {}
_SEEN_MSG_IDS: Dict[str, float] = {}
_SEEN_TTL_SEC = 300.0  # 5 minutos

def _load_intro_script() -> Dict[str, Any]:
    """Loads the intro script from a JSON file."""
    global _INTRO_SCRIPT
    if not _INTRO_SCRIPT:
        try:
            base = os.path.dirname(os.path.dirname(__file__))
            path = os.path.join(base, "rh_kelly_agent", "data", "roteiro_intro.json")
            # Tolerate UTF-8 BOM (files saved on Windows PowerShell)
            with open(path, "r", encoding="utf-8-sig") as f:
                _INTRO_SCRIPT = json.load(f)
        except Exception as exc:
            print(f"load intro script error: {exc}")
            _INTRO_SCRIPT = {"intro": [], "cta_labels": {}}
    return _INTRO_SCRIPT

def send_intro_message(destino: str, user_id: str, idx: int, nome: str) -> None:
    """Sends an intro message to the user (debounced)."""
    script = _load_intro_script()
    intro_messages = script.get("intro", [])
    if not (0 < idx <= len(intro_messages)):
        return

    # Debounce para evitar duplicidade em reentregas do webhook
    _ctx0 = _load_ctx(user_id) or {}
    try:
        _last = float(_ctx0.get("intro_sent_at") or 0.0)
        if _now() - _last < 10.0:
            return
    except Exception:
        pass

    safe_name = str(nome or "candidato(a)").strip()
    first_name = safe_name.split()[0] if safe_name else "candidato(a)"
    message = intro_messages[idx - 1]
    text = message.get("text", "").format(nome=first_name)

    cta_labels = script.get("cta_labels", {})
    next_label = cta_labels.get("next", "Avançar")
    skip_label = cta_labels.get("skip", "Pular")

    if idx == len(intro_messages):
        buttons = [("Sim", "Sim"), ("Não", "Não")]
    else:
        buttons = [("intro_next", next_label), ("ajuda", "Ajuda")]

    # Envia o texto longo e, em seguida, um corpo compacto com botões,
    # evitando que 'ajuda/menu' reenviem o texto longo da introducao.
    try:
        send_text_message(destino, text)
    except Exception:
        pass
    _fallback_next = "Avancar"
    body_compact = "Deseja prosseguir?" if idx == len(intro_messages) else str(next_label or _fallback_next)
    send_button_message_pairs(destino, body_compact, buttons)
    ctx = _load_ctx(user_id) or {}
    _set_last_menu(user_id, ctx, menu_type="buttons", body=body_compact, items=buttons)
    # Marca envio para debouncing
    try:
        ctx["intro_sent_at"] = _now()
        _save_ctx(user_id, ctx)
    except Exception:
        pass

def _handle_intro_action(destino: str, user_id: str, action: str) -> None:
    """Handles the user's action during the intro."""
    ctx = _load_ctx(user_id)
    current_idx = ctx.get("intro_idx", 1)
    nome = ctx.get("nome", "candidato(a)")
    if bool(ctx.get("from_intro")):
        ctx["stage"] = "await_city"
        _save_ctx(user_id, ctx)
        _send_city_menu(destino, user_id, ctx=ctx)
        return

    if action == "intro_next":
        next_idx = current_idx + 1
        script = _load_intro_script()
        intro_messages = script.get("intro", [])
        if next_idx <= len(intro_messages):
            ctx["intro_idx"] = next_idx
            ctx["stage"] = f"intro_{next_idx}"
            _save_ctx(user_id, ctx)
            send_intro_message(destino, user_id, next_idx, nome)
        else:
            ctx["stage"] = "await_city"
            ctx["from_intro"] = True
            _save_ctx(user_id, ctx)
            _send_city_menu(destino, user_id, ctx=ctx)

    elif action == "intro_skip":
        ctx["stage"] = "req_moto"
        _save_ctx(user_id, ctx)
        try:
            send_text_message(destino, "Perfeito! Antes de seguir, preciso confirmar alguns requisitos rapidos.")
        except Exception:
            pass
        _send_requirement_question(destino, "req_moto", user_id=user_id)

def _get_auth_headers() -> Dict[str, str]:
    """Obtém os cabeçalhos de autenticação para a API do WhatsApp."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

def _parse_first_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    # Caminho rápido: objeto JSON completo
    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            pass
    # Fallback: tenta extrair o primeiro objeto JSON dentro do texto
    try:
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(t[start:end+1])
    except Exception:
        pass
    return None

def send_text_message(destino: str, texto: str) -> None:
    """Envia uma mensagem de texto simples."""
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
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
        destino: telefone do destinatário (formato internacional, ex.: '5511999999999').
        corpo: texto exibido na mensagem (pergunta).
        botoes: lista de rótulos dos botões a serem exibidos.
    """
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"

    def _sanitize_button_title(txt: str, idx: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        if not t:
            t = f"Opção {idx+1}"
        return t[:20] if len(t) > 20 else t

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

def send_list_message(destino: str, corpo: str, opcoes: List[str], botao: str = "Ver opções") -> None:
    """Envia uma mensagem interativa do tipo "list" para mais de 3 opções."""
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"

    def _sanitize_row_title(txt: str, idx: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        if not t:
            t = f"Opção {idx+1}"
        return t[:24] if len(t) > 24 else t

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
                    {"title": "Cidades disponíveis", "rows": rows}
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
    """Envia botões com id e título separados."""
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"

    def _sanitize_title(t: str) -> str:
        s = (t or "").strip().replace("\n", " ")
        if not s:
            s = "Opção"
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

def send_list_message_rows(destino: str, corpo: str, rows_in: List[Any], botao: str = "Ver opções") -> None:
    """Envia lista com rows custom (id, title[, description])."""
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
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
            "action": {"button": botao, "sections": [{"rows": rows}]}
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
    """Heurística simples para extrair opções do texto do agente."""
    if not text:
        return []
    s = text
    import re
    m = re.search(r":\s*([^\n\r]+)$", s) or re.search(r":\s*([^.!?]+)[.!?]", s)
    if not m:
        return []
    region = m.group(1)
    region = region.replace(" ou ", ", ").replace(" e ", ", ")
    parts = [p.strip() for p in region.split(",") if p.strip()]
    parts = [p for p in parts if len(p) >= 2 and not any(ch.isdigit() for ch in p)]
    seen = set()
    out: List[str] = []
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out

# ADK Communication
_runner: Optional[Runner] = None
_session_service: Optional[InMemorySessionService] = None
_APP_NAME = "rh_kelly_agent"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runner, _session_service
    print("FastAPI app startup event: Initializing ADK Runner...")
    try:
        _session_service = InMemorySessionService()
        _runner = Runner(
            app_name=_APP_NAME,
            agent=root_agent,
            session_service=_session_service
        )
        print("FastAPI app startup event: Agent runner initialized successfully.")
    except Exception as e:
        print(f"FATAL: Agent runner initialization failed: {e}")
    yield
    print("FastAPI app shutdown event.")

async def enviar_mensagem_ao_agente_async(user_id: str, mensagem: str, stage: Optional[str] = None) -> Dict[str, Any]:
    """Versão assíncrona usando Runner.run_async e SessionService async."""
    if not _runner or not _session_service:
        raise HTTPException(status_code=503, detail="Agent runner not initialized")
    sess = await _session_service.get_session(
        app_name=_APP_NAME, user_id=user_id, session_id=user_id
    )
    if not sess:
        await _session_service.create_session(
            app_name=_APP_NAME, user_id=user_id, session_id=user_id
        )

    full_message = str(mensagem or "")
    if stage:
        full_message = f"Contexto atual: {stage}. Mensagem do usuário: {full_message}"
    content = genai_types.Content(parts=[genai_types.Part(text=full_message)])
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
            "options": parsed.get("options") if isinstance(parsed.get("options", []), list) else None,
        }
    return {"content": last_text or "", "options": None}

# Deterministic flow support
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
    _USER_CTX[user_id] = ctx
    if _r is not None:
        try:
            _r.setex(f"lead_ctx:{user_id}", _CTX_TTL_SEC, json.dumps(ctx, ensure_ascii=False))
        except Exception as exc:
            print(f"redis set ctx error: {exc}")

def _now() -> float:
    return time.time()

MAX_INVALID_PER_STAGE = int(os.environ.get("MAX_INVALID_PER_STAGE", "2"))
MAX_OFF_CONTEXT = int(os.environ.get("MAX_OFF_CONTEXT", "3"))
RECAP_AFTER_MINUTES = int(os.environ.get("RECAP_AFTER_MINUTES", "30"))

def _set_last_menu(user_id: str, ctx: Dict[str, Any], *, menu_type: str, body: str, items: List[Any], botao: Optional[str] = None) -> None:
    ctx["last_menu"] = {
        "type": menu_type,
        "body": body,
        "items": items,
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
        m = {str(x).strip().lower(): str(x) for x in items}
        _CITIES_CACHE.update({
            "expires": _now() + float(ttl_sec),
            "items": items,
            "map": m,
        })
    except Exception as exc:
        print(f"cities cache error: {exc}")
    return _CITIES_CACHE

def _match_city(label: str) -> Optional[str]:
    m = _get_cities_cached().get("map", {})
    return m.get(str(label or "").strip().lower())

def _send_turno_menu(destino: str, cidade: str) -> None:
    """Envia opções de turno disponíveis na cidade, de forma determinística."""
    try:
        res = verificar_vagas(cidade)
    except Exception as exc:
        print(f"verificar_vagas error: {exc}")
        send_text_message(destino, f"Cidade selecionada: {cidade}. Não foi possível consultar as vagas agora.")
        return
    if not isinstance(res, dict) or res.get("status") != "success":
        send_text_message(destino, f"Cidade selecionada: {cidade}. Não encontrei vagas abertas no momento.")
        return
    vagas = res.get("vagas") or []
    seen = set()
    turnos: List[str] = []
    for v in vagas:
        t = str((v or {}).get("turno", "")).strip()
        if t and t not in seen:
            seen.add(t)
            turnos.append(t)
    content = f"Cidade selecionada: {cidade}. Escolha um turno disponível:"
    if not turnos:
        send_text_message(destino, f"Cidade selecionada: {cidade}. Existem vagas, mas não consegui listar os turnos agora.")
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
    if "from_intro" in ctx:
        try:
            ctx.pop("from_intro", None)
        except Exception:
            pass
    ctx["stage"] = "req_moto"
    _save_ctx(user_id, ctx)
    try:
        send_text_message(destino, "Perfeito! Antes de seguir, preciso confirmar alguns requisitos rápidos.")
    except Exception:
        pass
    _send_requirement_question(destino, "req_moto", user_id=user_id)
    return {"handled": True}


def _handle_city_selection_reject(destino: str, user_id: str, selected: str) -> Dict[str, Any]:
    cidade = _match_city(selected)
    if not cidade:
        return {"handled": False}
    ctx = {**_load_ctx(user_id)}
    ctx.update({"cidade": cidade, "aprovado": False})
    _save_ctx(user_id, ctx)
    try:
        send_text_message(destino, f"Obrigado! Cidade registrada: {cidade}. Seus dados foram salvos para futuras oportunidades.")
    except Exception:
        pass
    _save_lead_record(user_id)
    ctx["stage"] = "final"
    _save_ctx(user_id, ctx)
    return {"handled": True}

def _send_city_menu(destino: str, user_id: str, ctx: Optional[Dict[str, Any]] = None, prompt: Optional[str] = None) -> None:
    if ctx is None:
        ctx = _load_ctx(user_id) or {}

    cache = _get_cities_cached()
    cities = cache.get("items", []) or []
    if not cities:
        send_text_message(destino, "No momento, não consegui obter as cidades com vagas.")
        return
    nome = ctx.get("nome", "candidato(a)")
    pergunta = prompt or ("Antes de come??armos, preciso saber: \\n" "Em qual cidade vocG atua como entregador?\\n" "Selecione no menu abaixo")
    pairs = [(c, c) for c in cities]
    if len(cities) > 3:
        send_list_message_rows(destino, pergunta, pairs, botao="Ver cidades")
        _set_last_menu(user_id, ctx, menu_type="list", body=pergunta, items=pairs, botao="Ver cidades")
    else:
        send_button_message_pairs(destino, pergunta, pairs)
        _set_last_menu(user_id, ctx, menu_type="buttons", body=pergunta, items=pairs)

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
        "text": "Você está no meio de sua rota diária quando surge uma nova coleta de alta prioridade em uma área próxima, mas que exigirá um desvio.",
        "options": [
            ("Q1_A", "Analiso o impacto no restante da minha rota e, se for viável, ajusto o percurso para incluir a nova coleta sem comprometer os outros prazos."),
            ("Q1_B", "Comunico-me imediatamente com a central para avaliar a melhor estratégia, seja repassar a coleta para outro entregador ou receber ajuda para reorganizar minhas entregas."),
            ("Q1_C", "Aceito o desafio e me apresso para realizar a coleta urgente primeiro, mesmo que isso signifique um possível atraso nas outras entregas.")
        ],
    },
    {
        "id": "Q2",
        "text": "Ao chegar no endereço de entrega, o cliente te liga e pede para deixar a encomenda com o vizinho, um procedimento que não é o padrão da empresa.",
        "options": [
            ("Q2_A", "Agradeço a instrução, mas informo que, por segurança, preciso seguir o procedimento padrão e peço para o cliente ou uma pessoa autorizada receber a encomenda no local correto."),
            ("Q2_B", "Entendo a necessidade do cliente e tento encontrar uma solução, como aguardar alguns minutos ou sugerir que ele autorize formalmente a entrega ao vizinho pelo aplicativo."),
            ("Q2_C", "Para agilizar, deixo com o vizinho conforme solicitado, garantindo que ele se responsabilize e informando o cliente que a encomenda foi deixada no local alternativo.")
        ],
    },
    {
        "id": "Q3",
        "text": "Uma chuva intensa e inesperada começa no meio do seu turno, diminuindo a visibilidade e tornando as ruas escorregadias.",
        "options": [
            ("Q3_A", "Imediatamente reduzo a velocidade e aumento a distância dos outros veículos. A segurança vem sempre em primeiro lugar."),
            ("Q3_B", "Procuro um local seguro para parar temporariamente, aviso a central sobre as condições climáticas e informo os clientes sobre possíveis atrasos."),
            ("Q3_C", "Continuo a rota com cuidado redobrado, mas tentando manter o ritmo para não comprometer os prazos.")
        ],
    },
    {
        "id": "Q4",
        "text": "Na farmácia, ao coletar um pedido, você nota que a embalagem de um item frágil está mal fechada e parece que pode abrir a qualquer momento.",
        "options": [
            ("Q4_A", "Comunico o problema imediatamente ao responsável da farmácia e peço que a embalagem seja trocada ou reforçada."),
            ("Q4_B", "Eu mesmo tento reforçar a embalagem da melhor forma possível para não perder tempo e seguir para a entrega."),
            ("Q4_C", "Informo o cliente sobre a condição da embalagem e pergunto se ele ainda deseja receber o pedido, explicando que farei o meu melhor para que chegue intacto.")
        ],
    },
    {
        "id": "Q5",
        "text": "Você tem duas coletas agendadas em locais próximos, mas com horários de retirada quase simultâneos e muito apertados.",
        "options": [
            ("Q5_A", "Verifico no mapa a rota mais rápida e ligo para os estabelecimentos para avisar de um possível pequeno atraso em um deles, negociando a melhor ordem de coleta."),
            ("Q5_B", "Sigo a ordem definida pelo sistema, focando em ser o mais ágil possível em cada parada para tentar cumprir ambos os horários."),
            ("Q5_C", "Analiso a situação e, se o risco de atraso for alto, peço ajuda à central para que outro entregador assuma uma das coletas.")
        ],
    },
]

_DISC_SCORES = {
    "Q1_A": 1, "Q1_B": 1, "Q1_C": 0,
    "Q2_A": 1, "Q2_B": 1, "Q2_C": 0,
    "Q3_A": 1, "Q3_B": 1, "Q3_C": 0,
    "Q4_A": 1, "Q4_B": 1, "Q4_C": 0,
    "Q5_A": 1, "Q5_B": 1, "Q5_C": 0,
}

_DISC_TRAIT_SCORES = {
    "Q1_A": {"S": 1, "C": 1},
    "Q1_B": {"I": 1, "S": 1},
    "Q1_C": {"D": 1},
    "Q2_A": {"C": 1},
    "Q2_B": {"I": 1, "S": 1},
    "Q2_C": {"D": 1},
    "Q3_A": {"C": 1, "S": 1},
    "Q3_B": {"S": 1, "I": 1},
    "Q3_C": {"D": 1},
    "Q4_A": {"C": 1},
    "Q4_B": {"D": 1, "S": 1},
    "Q4_C": {"I": 1},
    "Q5_A": {"I": 1, "C": 1},
    "Q5_B": {"D": 1},
    "Q5_C": {"S": 1, "C": 1},
}

def _send_disc_question(destino: str, q_idx: int, user_id: Optional[str] = None, ctx: Optional[Dict[str, Any]] = None) -> None:
    if user_id and ctx is None:
        ctx = _load_ctx(user_id) or {}
    elif ctx is None:
        ctx = {}

    q = _DISC_QUESTIONS[q_idx]
    
    full_text_message = f"Cenário: {q['text']}\n\nComo você agiria?\n"
    button_pairs = []
    for i, (_id, title) in enumerate(q["options"]):
        option_label = chr(65 + i)
        full_text_message += f"{option_label}) {title}\n"
        button_pairs.append((_id, f"Opção {option_label}"))

    send_text_message(destino, full_text_message)

    body_buttons = "Selecione uma opção abaixo:"
    send_button_message_pairs(destino, body_buttons, button_pairs)

    if user_id:
        _set_last_menu(user_id, ctx, menu_type="buttons", body=body_buttons, items=button_pairs)

def _map_disc_selection(q_idx: int, selected_label: str) -> Optional[str]:
    q = _DISC_QUESTIONS[q_idx]
    for _id, _ in q["options"]:
        if selected_label == _id:
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

def _send_vagas_menu(destino: str, cidade: str, user_id: Optional[str] = None, ctx: Optional[Dict[str, Any]] = None) -> None:
    if user_id and ctx is None:
        ctx = _load_ctx(user_id) or {}
    elif ctx is None:
        ctx = {}

    vagas = _fetch_vagas_by_city(cidade)
    if not vagas:
        send_text_message(destino, f"Aprovado! Porém, não encontrei vagas listadas agora para {cidade}.")
        return
    rows_labels = []
    for v in vagas:
        vid = str(v.get("vaga_id") or v.get("VAGA_ID") or "?")
        farm = str(v.get("farmacia") or v.get("FARMACIA") or "?")
        turno = str(v.get("turno") or v.get("TURNO") or "?")
        taxa = str(v.get("taxa_entrega") or v.get("TAXA_ENTREGA") or "?")
        rows_labels.append((vid, f"ID {vid}", f"Turno: {turno} | Farmácia: {farm} | Taxa: {taxa}"))
    body_list = "Selecione uma vaga no menu abaixo 👇"
    send_list_message_rows(destino, body_list, rows_labels, botao="Ver vagas")
    if user_id:
        _set_last_menu(user_id, ctx, menu_type="list", body=body_list, items=rows_labels, botao="Ver vagas")

def _find_vaga_by_row_title(cidade: str, title_or_id: str) -> Optional[Dict[str, Any]]:
    vagas = _fetch_vagas_by_city(cidade)
    t = (title_or_id or "").strip()
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
            "disc_score": ctx.get('disc_score'),
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
                    "PERFIL_APROVADO": "Sim" if aprovado else "N�o",
                    "PERFIL_NOTA": score,
                    "PROTOCOLO": protocolo,
                    "TURNO_ESCOLHIDO": turno,
                    "VAGA_ID": row.get("vaga_id"),
                    "FARMACIA": row.get("farmacia"),
                    "CIDADE": row.get("cidade"),
                    "TAXA_ENTREGA": row.get("taxa_entrega"),
                    "ANALISE_PERFIL": ctx.get('analise_perfil'),
                }
                values = [mapping.get(h, row.get(h)) for h in header]
                ws.append_row(values, value_input_option="USER_ENTERED")
            except Exception as ws_exc:
                print(f"sheets append error: {ws_exc}")
        if _r is not None:
            try:
                _r.rpush("leads_records", json.dumps(row, ensure_ascii=False))
                _r.set(f"lead_final:{user_id}", json.dumps(row, ensure_ascii=False))
            except Exception as rex:
                print(f"redis save lead error: {rex}")
    except Exception as exc:
        print(f"save lead error: {exc}")

def _download_whatsapp_media(media_id: str) -> Optional[Dict[str, Any]]:
    try:
        token = os.environ["WHATSAPP_ACCESS_TOKEN"]
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
        binr = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        binr.raise_for_status()
        return {"bytes": binr.content, "mime_type": mime or "audio/ogg"}
    except Exception as exc:
        print(f"download media error: {exc}")
        return None

def _transcribe_audio_gemini(data: bytes, mime_type: str) -> Optional[str]:
    try:
        model_name = os.environ.get("AUDIO_TRANSCRIBE_MODEL") or AGENT_MODEL
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
    """Processa a resposta do ADK e envia ao usuário via WhatsApp."""
    content = resposta.get("content")
    options = resposta.get("options")
    if not options:
        inferred = _extract_options_from_text(content or "")
        if len(inferred) >= 2:
            options = inferred

    if options:
        if len(options) > 3:
            send_list_message(destino, content or "Selecione uma opção:", options)
        else:
            send_button_message(destino, content or "Selecione uma opção:", options)
    else:
        send_text_message(destino, content or "Desculpe, não consegui entender.")

# FastAPI Web Server
app = FastAPI(lifespan=lifespan)

@app.get("/")
def healthcheck():
    return {"status": "ok"}

@app.get("/webhook")
def verify_webhook(request: Request):
    """Endpoint para a verificação do webhook do WhatsApp."""
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
    data = await request.json()
    try:
        entry = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        messages = entry.get("messages")
        if not messages:
            return {"status": "ignored"}
        msg = messages[0]
        from_number = msg.get("from", "")
        try:
            msg_id = msg.get("id")
            if msg_id:
                if _r is not None:
                    try:
                        ok = _r.set(f"seen_msg:{msg_id}", "1", nx=True, ex=int(_SEEN_TTL_SEC))
                        if not ok:
                            return {"status": "handled_duplicate"}
                    except Exception as _rexc:
                        print(f"redis dedup error: {_rexc}")
                else:
                    now = time.time()
                    try:
                        expired = [k for k, ts in list(_SEEN_MSG_IDS.items()) if ts < now]
                        for k in expired:
                            _SEEN_MSG_IDS.pop(k, None)
                    except Exception:
                        pass
                    if msg_id in _SEEN_MSG_IDS:
                        return {"status": "handled_duplicate"}
                    _SEEN_MSG_IDS[msg_id] = now + _SEEN_TTL_SEC
        except Exception:
            pass
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
                    if bid in ("intro_next", "intro_skip"):
                        _handle_intro_action(from_number, from_number, bid)
                        return {"status": "handled"}
                elif itype == "list_reply":
                    lr = interactive.get("list_reply", {})
                    lid = lr.get("id")
                    ltitle = lr.get("title", "")
                    texto_usuario = lid or ltitle
            elif mtype == "audio":
                media = msg.get("audio", {}) or {}
                mid = media.get("id")
                if mid:
                    try:
                        mdat = _download_whatsapp_media(mid)
                        if mdat and mdat.get("bytes"):
                            texto_usuario = _transcribe_audio_gemini(mdat["bytes"], mdat.get("mime_type") or "audio/ogg") or ""
                            was_audio = True
                    except Exception as aexc:
                        print(f"audio handle error: {aexc}")
        except Exception:
            texto_usuario = ""

        if not (texto_usuario or "").strip():
            if was_audio:
                try:
                    send_text_message(from_number, "Não consegui entender seu áudio. Pode escrever a mensagem?")
                except Exception:
                    pass
            return {"status": "ignored"}

        ctx = _load_ctx(from_number) or {}
        stage = ctx.get("stage")

        INTRO_BEFORE_CITY = _env_true("INTRO_BEFORE_CITY", default=True)

        if not stage and INTRO_BEFORE_CITY:
            ctx["stage"] = "intro_1"
            ctx["intro_idx"] = 1
            ctx["invalid_count"] = 0
            ctx["off_context_count"] = 0
            ctx["last_message_at"] = _now()
            _save_ctx(from_number, ctx)
            send_intro_message(from_number, from_number, 1, ctx.get("nome", "candidato(a)"))
            return {"status": "handled"}

        if not stage:
            ctx["stage"] = "await_city"
            ctx["invalid_count"] = 0
            ctx["off_context_count"] = 0
            ctx["last_message_at"] = _now()
            _save_ctx(from_number, ctx)
            _send_city_menu(from_number, from_number, ctx=ctx)
            return {"status": "handled"}

        if stage == "final":
            send_text_message(from_number, "O atendimento foi finalizado. Em breve, alguém da nossa equipe entrará em contato pelos canais oficiais de atendimento da CoopMob.")
            return {"status": "handled"}
        
        # Early handle: if user declines during intro, collect city for registry
        try:
            st_intro = str(ctx.get("stage") or "")
            yn_pre = _normalize_yes_no(_strip_accents(texto_usuario))
        except Exception:
            yn_pre = None
        if st_intro.startswith("intro_") and yn_pre is False:
            ctx["stage"] = "await_city_reject"
            _save_ctx(from_number, ctx)
            prompt = (
                "Antes de encerrar, em qual cidade você atua como entregador?\n"
                "Selecione uma opção abaixo"
            )
            _send_city_menu(from_number, from_number, ctx=ctx, prompt=prompt)
            return {"status": "handled"}

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

        def _cmd(txt: str) -> str:
            t = (txt or "").strip().lower()
            if t in {"menu"}: return "menu"
            if t in {"voltar"}: return "voltar"
            if t in {"recomecar", "recomeçar"}: return "recomecar"
            if t in {"ajuda", "help"}: return "ajuda"
            if t in {"humano", "atendente", "suporte"}: return "humano"
            if t in {"status", "progresso"}: return "status"
            if t in {"comandos", "comando", "help comandos"}: return "comandos"
            return ""

        cmd = _cmd(texto_usuario)
        if cmd == "recomecar":
            ctx = {"stage": "await_city", "invalid_count": 0, "off_context_count": 0, "last_message_at": _now()}
            _save_ctx(from_number, ctx)
            _send_city_menu(from_number, from_number, ctx=ctx)
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
            if st.startswith("intro"):
                try:
                    qi = int(st.replace("intro_", ""))
                except Exception:
                    qi = 0
                if qi > 1:
                    back_to(f"intro_{qi-1}")
                    send_intro_message(from_number, from_number, qi-1, ctx.get("nome", "candidato(a)"))
                else:
                    back_to("await_city")
                    _send_city_menu(from_number, from_number, ctx=ctx)
                return {"status": "handled"}
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
                _resend_last_menu(from_number, ctx) or _send_vagas_menu(from_number, ctx.get("cidade") or "", user_id=from_number)
                return {"status": "handled"}
            if st == "req_android":
                back_to("req_cnh"); _send_requirement_question(from_number, "req_cnh", user_id=from_number); return {"status": "handled"}
            if st == "req_cnh":
                back_to("req_moto"); _send_requirement_question(from_number, "req_moto", user_id=from_number); return {"status": "handled"}
            if st == "req_moto":
                back_to("await_city"); _send_city_menu(from_number, from_number, ctx=ctx); return {"status": "handled"}
            if _resend_last_menu(from_number, ctx):
                return {"status": "handled"}
        if cmd == "ajuda":
            st = str(ctx.get("stage") or "")
            tips = {
                "await_city": "Toque em uma das cidades do menu para continuar.",
                "req_moto": "Responda tocando em Sim ou Não.",
                "req_cnh": "Responda tocando em Sim ou Não.",
                "req_android": "Responda tocando em Sim ou Não.",
                "offer_positions": "Toque em uma vaga do menu para selecionar.",
            }
            send_text_message(from_number, "Ajuda: " + (tips.get(st, "Selecione uma opcao do menu abaixo.")) + "\nDigite 'comandos' para ver a lista completa de comandos.")
            _resend_last_menu(from_number, ctx)
        if cmd == "comandos":
            guide = (
                "Guia rapido de comandos:\n"
                "- menu: reenvia o ultimo menu\n"
                "- voltar: volta uma etapa\n"
                "- recomecar: inicia do zero\n"
                "- status: mostra etapa, cidade, requisitos e progresso\n"
                "- ajuda: dica da etapa atual\n"
                "- humano: encaminhar para atendimento humano\n\n"
                "Dica: responda tocando nas opcoes quando possivel."
            )
            send_text_message(from_number, guide)
            if ctx.get("last_menu"): _resend_last_menu(from_number, ctx)
            return {"status": "handled"}
        if cmd == "status":
            st_map = {
                "await_city": "Aguardando seleção de cidade",
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
            if ctx.get('disc_score') is not None:
                msg += f"• Pontuação DISC: {ctx.get('disc_score')}\n"
            if ctx.get('analise_perfil'):
                msg += f"• Análise de Perfil:\n{ctx.get('analise_perfil')}\n"
            msg += "\nDicas: digite 'menu' para ver as opções, 'voltar' para a etapa anterior ou 'recomeçar' para iniciar do zero."
            send_text_message(from_number, msg)
            if ctx.get("last_menu"):
                _resend_last_menu(from_number, ctx)
            return {"status": "handled"}
        if cmd == "humano":
            send_text_message(from_number, "Sem problemas! Vou pedir para nossa equipe te chamar. Você também pode preencher o formulário: https://app.pipefy.com/public/form/v2m7kpB-")
            _save_lead_record(from_number)
            ctx["stage"] = "final"; _save_ctx(from_number, ctx)
            return {"status": "handled"}

        try:
            st_local = str(ctx.get("stage") or ""); handled = {"handled": False}; handled = _handle_city_selection(from_number, from_number, texto_usuario) if st_local == "await_city" else (_handle_city_selection_reject(from_number, from_number, texto_usuario) if st_local == "await_city_reject" else {"handled": False})
            if handled.get("handled"):
                return {"status": "handled"}
        except Exception as sel_exc:
            print(f"city selection handler error: {sel_exc}")

        try:
            if stage.startswith("intro_"):
                yn = _normalize_yes_no(_strip_accents(texto_usuario))
                if yn is True:
                    ctx["stage"] = "await_city"
                    ctx["from_intro"] = True
                    _save_ctx(from_number, ctx)
                    _send_city_menu(from_number, from_number, ctx=ctx)
                    return {"status": "handled"}
                if yn is False:
                    send_text_message(from_number, "Tudo bem. Fico a disposição para futuras oportunidades. Obrigada!")
                    ctx["stage"] = "final"
                    _save_ctx(from_number, ctx)
                    return {"status": "handled"}
                _resend_last_menu(from_number, ctx)
                return {"status": "handled"}

            if stage == "req_moto":
                yn = _normalize_yes_no(_strip_accents(texto_usuario))
                if yn is not None:
                    ctx["req_moto"] = bool(yn)
                    ctx["stage"] = "req_cnh"
                    _save_ctx(from_number, ctx)
                    send_text_message(from_number, "Ótimo, obrigada pela confirmação.")
                    _send_requirement_question(from_number, "req_cnh", user_id=from_number)
                    return {"status": "handled"}

            if stage == "req_cnh":
                yn = _normalize_yes_no(_strip_accents(texto_usuario))
                if yn is not None:
                    ctx["req_cnh"] = bool(yn)
                    ctx["stage"] = "req_android"
                    _save_ctx(from_number, ctx)
                    send_text_message(from_number, "Perfeito, mais uma pergunta rápida.")
                    _send_requirement_question(from_number, "req_android", user_id=from_number)
                    return {"status": "handled"}

            if stage == "req_android":
                yn = _normalize_yes_no(_strip_accents(texto_usuario))
                if yn is not None:
                    ctx["req_android"] = bool(yn)
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
                        _send_disc_question(from_number, q_idx+1, user_id=from_number, ctx=ctx)
                    else:
                        score = sum(_DISC_SCORES.get(a, 0) for a in answers)
                        ctx["disc_score"] = score
                        trait_scores = {"D": 0, "I": 0, "S": 0, "C": 0}
                        for ans_id in answers:
                            traits = _DISC_TRAIT_SCORES.get(ans_id, {})
                            for trait, points in traits.items():
                                trait_scores[trait] += points
                        ctx["disc_trait_scores"] = trait_scores
                        profile_desc = "Perfil do Candidato:\n"
                        for trait, score in trait_scores.items():
                            profile_desc += f"- {trait}: {score} pontos\n"
                        dominant_traits = [t for t, s in trait_scores.items() if s == max(trait_scores.values())]
                        if dominant_traits:
                            profile_desc += "\nTraços dominantes: " + ", ".join(dominant_traits) + ".\n"
                            if "D" in dominant_traits: profile_desc += "Indica foco em resultados e proatividade.\n"
                            if "I" in dominant_traits: profile_desc += "Indica habilidade de comunicação e persuasão.\n"
                            if "S" in dominant_traits: profile_desc += "Indica estabilidade e paciência.\n"
                            if "C" in dominant_traits: profile_desc += "Indica atenção a detalhes e conformidade.\n"
                        else:
                            profile_desc += "Não foi possível identificar traços dominantes claros.\n"
                        ctx["analise_perfil"] = profile_desc
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
                    link_url = "https://app.pipefy.com/public/form/v2m7kpB-"
                    det_vid = ctx["vaga"].get("VAGA_ID")
                    det_farm = ctx["vaga"].get("FARMACIA")
                    det_turno = ctx["vaga"].get("TURNO")
                    det_taxa = ctx["vaga"].get("TAXA_ENTREGA")
                    send_text_message(from_number, (
                        f"Vaga selecionada:\n"
                        f"• ID: {det_vid}\n• Farmácia: {det_farm}\n• Turno: {det_turno}\n• Taxa: {det_taxa}"
                    ))
                    send_text_message(from_number, (
                        f"Excelente! Sua manifestação de interesse na vaga ID {det_vid} foi registrada com sucesso.\n"
                        f"Para dar o próximo passo em sua jornada de associação à CoopMob, por favor, preencha o formulário de cadastro: {link_url}.\n\n"
                        "Nossa equipe entrará em contato em breve para dar continuidade ao seu processo de ingresso na cooperativa. Agradecemos seu interesse em fazer parte da nossa comunidade de entregadores cooperados!"
                    ))
                    _save_lead_record(from_number)
                    ctx["stage"] = "final"
                    _save_ctx(from_number, ctx)
                    return {"status": "handled"}
                else:
                    send_text_message(from_number, "Não entendi a vaga selecionada. Por favor, escolha uma das opções do menu de vagas.")
                    _send_vagas_menu(from_number, cidade, user_id=from_number, ctx=ctx)
                    return {"status": "handled"}
        except Exception as exc:
            print(f"flow error: {exc}")

        try:
            st = str(stage or "")
            deterministic = (
                (not st)
                or st.startswith("intro_")
                or st in {"await_city", "req_moto", "req_cnh", "req_android", "offer_positions"}
                or st.startswith("disc_q")
            )
            if deterministic:
                if not _resend_last_menu(from_number, ctx):
                    pass
                return {"status": "handled"}
        except Exception:
            pass
        try:
            agent_response = await enviar_mensagem_ao_agente_async(from_number, texto_usuario, stage=stage)
            processar_resposta_do_agente(from_number, agent_response)
        except Exception as inner_exc:
            print(f"Agent pipeline error: {inner_exc}")
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

# Test endpoints
class SendTextRequest(BaseModel):
    to: str
    text: str

@app.post("/send-text")
def send_text_endpoint(payload: SendTextRequest, authorization: Optional[str] = Header(default=None)):
    """Endpoint opcional para disparar uma mensagem de texto de teste."""
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

@app.get("/llm-ping")
def llm_ping():
    """Executa uma chamada mínima ao modelo Gemini para verificar conectividade."""
    try:
        model_name = AGENT_MODEL
        model = genai.GenerativeModel(model_name)
        resp = model.generate_content("ping")
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
    """Endpoint para disparar mensagem com botões (máx 3) para testes."""
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
    if not _runner or not _session_service:
        return {"status": "error", "error": "Agent runner not initialized"}
    uid = user_id or "diagnostic-user"
    msg = text or "ping"
    try:
        try:
            _ = _session_service.get_session_sync(app_name=_APP_NAME, user_id=uid, session_id=uid)
        except Exception:
            _ = None
        if not _:
            _session_service.create_session_sync(app_name=_APP_NAME, user_id=uid, session_id=uid)

        content = genai_types.Content(parts=[genai_types.Part(text=msg)])
        last_text = None
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)










