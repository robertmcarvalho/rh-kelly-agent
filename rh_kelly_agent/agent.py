"""Definition of the RH Kelly recruitment agent with Redis memory backend."""

from __future__ import annotations

import csv
import io
import json
import os
import unicodedata
import urllib.request
from typing import Dict, List, Optional

from google.adk.agents import Agent
from .config import AGENT_MODEL
import google.generativeai as genai

try:
    import google.genai as genai2
except ImportError:
    genai2 = None

try:
    from google.adk.memory import RedisMemory
except ImportError:
    RedisMemory = None

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

_USE_VERTEX = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").strip().upper() == "TRUE"
_API_KEY = os.environ.get("GOOGLE_API_KEY")
try:
    if not _USE_VERTEX and _API_KEY:
        if genai:
            genai.configure(api_key=_API_KEY)
        if genai2 and hasattr(genai2, "configure"):
            genai2.configure(api_key=_API_KEY)
except Exception as _cfg_exc:
    print(f"genai configure error: {_cfg_exc}")

SHEET_ID = "1DESD3YZwOX0vwbelz5vJ6QJybuhPnjUMLhTlYblQt_c"
VAGAS_GID = "0"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={VAGAS_GID}"

def _fetch_vagas_csv() -> List[Dict[str, str]]:
    """Baixa e parseia a aba 'Vagas' como lista de dicionários."""
    with urllib.request.urlopen(CSV_URL) as response:
        csv_data = response.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_data))
    return list(reader)

def _slugify(text: str) -> str:
    """Normaliza uma string para slug (acentos -> ASCII, espaços -> hífens)."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return normalized.lower().replace(" ", "-")

def listar_cidades_com_vagas() -> Dict[str, object]:
    """Retorna cidades com vagas abertas (STATUS = 'Aberto' e VAGAS_RESTANTES >= 1)."""
    try:
        rows = _fetch_vagas_csv()
        cidades: set[str] = set()
        for row in rows:
            status = row.get("STATUS", "").strip().lower()
            vagas_restantes = row.get("VAGAS_RESTANTES")
            if status == "aberto":
                try:
                    if vagas_restantes is None or float(vagas_restantes) >= 1:
                        cidade = row.get("CIDADE", "").strip()
                        if cidade:
                            cidades.add(cidade)
                except (ValueError, TypeError):
                    cidade = row.get("CIDADE", "").strip()
                    if cidade:
                        cidades.add(cidade)
        return {"status": "success", "cidades": sorted(cidades)}
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}

def verificar_vagas(cidade: str) -> Dict[str, object]:
    """Retorna os turnos e detalhes das vagas abertas para a cidade informada."""
    try:
        rows = _fetch_vagas_csv()
        resultados: List[Dict[str, object]] = []
        for row in rows:
            if (
                row.get("CIDADE", "").strip().lower() == cidade.lower()
                and row.get("STATUS", "").strip().lower() == "aberto"
            ):
                vagas_restantes = row.get("VAGAS_RESTANTES")
                try:
                    if vagas_restantes is None or float(vagas_restantes) >= 1:
                        resultados.append({
                            "turno": row.get("TURNO", "").strip(),
                            "vagas_restantes": row.get("VAGAS_RESTANTES"),
                            "vaga_id": row.get("VAGA_ID"),
                            "farmacia": row.get("FARMACIA"),
                            "taxa_entrega": row.get("TAXA_ENTREGA"),
                        })
                except (ValueError, TypeError):
                    resultados.append({
                        "turno": row.get("TURNO", "").strip(),
                        "vagas_restantes": row.get("VAGAS_RESTANTES"),
                        "vaga_id": row.get("VAGA_ID"),
                        "farmacia": row.get("FARMACIA"),
                        "taxa_entrega": row.get("TAXA_ENTREGA"),
                    })
        if not resultados:
            return {"status": "error", "error_message": f"Nenhuma vaga encontrada para {cidade}."}
        return {"status": "success", "vagas": resultados}
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}

def aplicar_disc(pergunta_idx: int, resposta_selecionada: str) -> Dict[str, object]:
    """Processa a resposta do questionário DISC (versão simplificada)."""
    try:
        resultado = {
            "pergunta_idx": pergunta_idx,
            "resposta": resposta_selecionada,
        }
        return {"status": "success", "resultado": resultado}
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}

def registrar_interesse_pipeline(candidato: Dict[str, object]) -> Dict[str, object]:
    """Registra os dados do candidato em um arquivo JSON Lines."""
    try:
        log_path = os.path.join(DATA_DIR, "interest_log.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            json.dump(candidato, f, ensure_ascii=False)
            f.write("\n")
        return {"status": "success", "message": "Interesse registrado."}
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}

def enviar_link_pipefy(cidade: str) -> Dict[str, object]:
    """Retorna o link de inscrição no Pipefy para a cidade selecionada."""
    try:
        slug = _slugify(cidade)
        link = f"https://pipefy.com/form/{slug}"
        return {"status": "success", "link": link}
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}

root_agent = Agent(
    name="rh_kelly_agent",
    model=AGENT_MODEL,
    description="Agente de recrutamento da cooperativa de entregadores RH Kelly.",
    instruction="",
    tools=[
        listar_cidades_com_vagas,
        verificar_vagas,
        aplicar_disc,
        registrar_interesse_pipeline,
        enviar_link_pipefy,
    ],
)

root_agent.instruction = """
Voce e a Kelly, especialista em associacao de entregadores da nossa cooperativa.
Seja acolhedora, clara e trate o candidato pelo primeiro nome.

Se o usuario fizer uma pergunta fora do fluxo principal, tente responder da melhor forma possivel.
Nunca invente respostas. Se você não sabe a resposta para uma pergunta, diga que não possui essa informação e forneca o contato de suporte: (34) 99676-9726.
Apos responder a pergunta ou fornecer o contato de suporte, guie o usuario de volta ao fluxo principal.

Ao iniciar a conversa, cumprimente e apresente-se como Kelly. Em seguida, utilize listar_cidades_com_vagas()
para obter a lista de cidades com vagas abertas (STATUS='Aberto').

FORMATO DE SAIDA (OBRIGATORIO): Responda EXCLUSIVAMENTE com um JSON em uma unica linha, no formato
{"content": <string>, "options": <lista de strings ou []>}.
- "content": texto a ser enviado ao usuario.
- "options": quando desejar que o sistema apresente botoes/lista no WhatsApp, preencha com os rotulos das opcoes
(use exatamente os nomes canonicos das cidades retornadas pelas ferramentas). Caso nao existam, use [].
Nao inclua nenhuma outra palavra fora do JSON. Nao use markdown.

Fluxo: so prossiga para a proxima etapa do funil apos o candidato selecionar a cidade.
Siga o funil utilizando as funcoes (listar_cidades_com_vagas, verificar_vagas, aplicar_disc,
registrar_interesse_pipeline, enviar_link_pipefy) para obter informacoes e registrar progresso.
"""

redis_url = os.environ.get("REDIS_URL")
if redis_url and RedisMemory is not None:
    try:
        root_agent.memory_backend = RedisMemory(url=redis_url)
    except Exception as _mem_exc:
        print(f"RedisMemory init error: {_mem_exc}")