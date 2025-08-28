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
# from adk.memory import RedisMemory  # importa o backend Redis

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# IDs e URL da planilha de vagas
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
                except Exception:
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
                except Exception:
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

def buscar_conhecimento(topico: str, subtopico: Optional[str] = None) -> Dict[str, object]:
    """Retorna o conteúdo de um tópico/subtópico de conhecimento."""
    try:
        with open(os.path.join(DATA_DIR, "conhecimento.json"), "r", encoding="utf-8") as f:
            entries = json.load(f)
        for entry in entries:
            if entry.get("topico") == topico and (
                subtopico is None or entry.get("subtopico") == subtopico
            ):
                return {"status": "success", "content": entry.get("content", "")}
        return {"status": "error", "error_message": f"Conteúdo não encontrado para {topico}/{subtopico}"}
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

# Cria o agente principal com instruções em português
root_agent = Agent(
    name="rh_kelly_agent",
    model="gemini-1.5-flash",
    description="Agente de recrutamento da cooperativa de entregadores RH Kelly.",
    instruction=(
        "Você é a Kelly, especialista em recrutamento da nossa cooperativa de entregas. "
        "Seja acolhedora, clara e sempre trate o candidato pelo primeiro nome. Ao iniciar "
        "a conversa, cumprimente o candidato e apresente-se como Kelly. Em seguida, chame a "
        "função listar_cidades_com_vagas() para obter a lista de cidades com vagas abertas "
        "(STATUS = 'Aberto') e apresente essas opções como botões para que o candidato "
        "possa escolher a cidade onde atua. Só prossiga para a próxima etapa do funil após "
        "o candidato selecionar a cidade. Siga o funil descrito no documento de "
        "especificação utilizando as funções disponíveis (listar_cidades_com_vagas, "
        "verificar_vagas, buscar_conhecimento, aplicar_disc, registrar_interesse_pipeline, "
        "enviar_link_pipefy) para obter informações e registrar progresso."
    ),
    tools=[
        listar_cidades_com_vagas,
        verificar_vagas,
        buscar_conhecimento,
        aplicar_disc,
        registrar_interesse_pipeline,
        enviar_link_pipefy,
    ],
)

# Configura o backend de memória para usar Redis. A URI é lida da variável REDIS_URL.
redis_url = os.environ.get("REDIS_URL")
if redis_url:
    root_agent.memory_backend = RedisMemory(url=redis_url)
