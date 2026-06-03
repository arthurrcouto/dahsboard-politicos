#!/usr/bin/env python3
"""
coletar_dados.py
================
Coleta dados da API pública da Câmara dos Deputados e gera arquivos JSON
estáticos para o Painel Político BR.

Arquivos gerados em ./data/:
  deputados.json          — lista completa de deputados (513 + metadados)
  partidos.json           — lista de partidos com contagem de cadeiras
  votacoes.json           — últimas 100 votações do plenário
  proposicoes.json        — últimas 50 proposições (PLs recentes)
  senadores.json          — senadores em exercício
  despesas_summary.json   — top 20 deputados por gasto CEAP (ano atual)
  detalhes/{id}.json      — detalhe completo de cada deputado
  meta.json               — timestamp da última atualização

Uso:
  python coletar_dados.py              # coleta tudo
  python coletar_dados.py --lista      # só lista de deputados (rápido)
  python coletar_dados.py --detalhe 204536  # detalhe de um deputado específico
"""

import os
import sys
import json
import time
import logging
import argparse
import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Configuração ────────────────────────────────────────────────────────────
BASE_URL   = "https://dadosabertos.camara.leg.br/api/v2"
DATA_DIR   = Path("data")
DETALHE_DIR = DATA_DIR / "detalhes"
LOG_FORMAT  = "%(asctime)s [%(levelname)s] %(message)s"

# Anos do mandato atual (58ª legislatura)
MANDATO_INICIO = "2023-02-01"
MANDATO_ANOS   = [2023, 2024, 2025, 2026]

# Tipos de proposição substantivos
TIPOS_PL = ["PL", "PLP", "PEC", "PDC", "PRC"]

# Delay entre requests (segundos) — respeita rate limit da Câmara
DELAY_REQ  = 0.3   # entre requests normais
DELAY_PAGE = 0.5   # entre páginas de paginação

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)


# ─── HTTP Session com retry ───────────────────────────────────────────────────
def make_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,          # 1.5s, 3s, 6s, 12s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({"Accept": "application/json"})
    return session


SESSION = make_session()


def get(url: str, params: dict = None) -> dict:
    """GET com retry e rate limit."""
    try:
        r = SESSION.get(url, params=params, timeout=30)
        r.raise_for_status()
        time.sleep(DELAY_REQ)
        return r.json()
    except requests.HTTPError as e:
        if e.response.status_code == 429:
            log.warning("Rate limit — aguardando 10s...")
            time.sleep(10)
            return get(url, params)
        log.error(f"HTTP {e.response.status_code}: {url}")
        return {}
    except Exception as e:
        log.error(f"Erro em GET {url}: {e}")
        return {}


def get_all_pages(url: str, params: dict = None, max_pages: int = 50) -> list:
    """Busca todas as páginas de um endpoint paginado."""
    params = params or {}
    params.setdefault("itens", 100)
    results = []
    page = 1

    while page <= max_pages:
        params["pagina"] = page
        data = get(url, params)
        lote = data.get("dados", [])
        results.extend(lote)

        # Verifica se há próxima página
        links = data.get("links", [])
        has_next = any(l.get("rel") == "next" for l in links)
        if not has_next or len(lote) < params["itens"]:
            break

        page += 1
        time.sleep(DELAY_PAGE)

    return results


def save(path: Path, data) -> None:
    """Salva JSON com indentação."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"Salvo: {path} ({path.stat().st_size // 1024}KB)")


# ─── Coleta: Lista de Deputados ───────────────────────────────────────────────
def coletar_deputados() -> list:
    log.info("Coletando deputados...")
    data = get(f"{BASE_URL}/deputados", {"itens": 513, "ordem": "ASC", "ordenarPor": "nome"})
    deputados = data.get("dados", [])
    log.info(f"  {len(deputados)} deputados encontrados")
    return deputados


# ─── Coleta: Partidos ─────────────────────────────────────────────────────────
def coletar_partidos() -> list:
    log.info("Coletando partidos...")
    data = get(f"{BASE_URL}/partidos", {"itens": 100, "ordem": "ASC", "ordenarPor": "sigla"})
    return data.get("dados", [])


# ─── Coleta: Votações ─────────────────────────────────────────────────────────
def coletar_votacoes() -> list:
    log.info("Coletando votações...")
    ano_atual = datetime.date.today().year
    votacoes = []
    for ano in [ano_atual, ano_atual - 1]:
        data = get(f"{BASE_URL}/votacoes", {
            "itens": 50,
            "ordem": "DESC",
            "ordenarPor": "dataHoraRegistro",
            "dataInicio": f"{ano}-01-01",
            "dataFim": f"{ano}-12-31",
        })
        votacoes.extend(data.get("dados", []))
        if len(votacoes) >= 100:
            break
    return votacoes[:100]


# ─── Coleta: Proposições ──────────────────────────────────────────────────────
def coletar_proposicoes() -> list:
    log.info("Coletando proposições...")
    ano = datetime.date.today().year
    data = get(f"{BASE_URL}/proposicoes", {
        "itens": 50,
        "ordem": "DESC",
        "ordenarPor": "id",
        "siglaTipo": "PL",
        "ano": ano,
    })
    return data.get("dados", [])


# ─── Coleta: Senadores ────────────────────────────────────────────────────────
def coletar_senadores() -> list:
    log.info("Coletando senadores...")
    try:
        r = SESSION.get(
            "https://legis.senado.leg.br/dadosabertos/senador/lista/atual.json",
            timeout=30,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        lista = (
            data.get("ListaParlamentarEmExercicio", {})
                .get("Parlamentares", {})
                .get("Parlamentar", [])
        )
        senadores = []
        for s in lista:
            p = s.get("IdentificacaoParlamentar", {})
            m = s.get("Mandato", {})
            senadores.append({
                "id":       p.get("CodigoParlamentar"),
                "nome":     p.get("NomeParlamentar"),
                "partido":  p.get("SiglaPartidoParlamentar"),
                "uf":       p.get("UfParlamentar"),
                "foto":     p.get("UrlFotoParlamentar"),
                "email":    p.get("EmailParlamentar"),
                "mandato":  m.get("DescricaoParticipacao"),
            })
        log.info(f"  {len(senadores)} senadores encontrados")
        return senadores
    except Exception as e:
        log.error(f"Erro ao buscar senadores: {e}")
        return []


# ─── Coleta: Detalhe de um Deputado ──────────────────────────────────────────
def coletar_detalhe_deputado(dep_id: int) -> dict:
    """
    Coleta detalhe completo de um deputado:
    - dados básicos
    - total de proposições (todos os tipos) por ano
    - PLs substantivos por ano
    - aprovadas (cod 1140)
    - discursos no mandato
    - presença em eventos por ano
    - despesas CEAP (total por mês)
    """
    log.info(f"  Detalhe deputado {dep_id}...")
    dep_id = int(dep_id)

    # Dados básicos + mandatos (paralelo)
    detalhes = get(f"{BASE_URL}/deputados/{dep_id}")
    mandatos = get(f"{BASE_URL}/deputados/{dep_id}/mandatos")

    # ── Proposições ──────────────────────────────────────────────────────────
    total_props = 0
    total_pls   = 0
    props_por_ano = {}

    for ano in MANDATO_ANOS:
        # Total (todos os tipos) — pagina completo
        t = len(get_all_pages(
            f"{BASE_URL}/proposicoes",
            {"idDeputadoAutor": dep_id, "ano": ano},
            max_pages=40,
        ))
        # PLs substantivos — 1 req por tipo
        p = 0
        for tipo in TIPOS_PL:
            r = get(f"{BASE_URL}/proposicoes", {
                "idDeputadoAutor": dep_id,
                "ano": ano,
                "siglaTipo": tipo,
                "itens": 1,
            })
            links = r.get("links", [])
            last  = next((l["href"] for l in links if l.get("rel") == "last"), "")
            if "pagina=" in last and "itens=" in last:
                import re
                pg = int(re.search(r"pagina=(\d+)", last).group(1))
                it = int(re.search(r"itens=(\d+)",  last).group(1))
                p += pg * it
            else:
                p += len(r.get("dados", []))
            time.sleep(0.1)

        props_por_ano[str(ano)] = {"total": t, "pls": p}
        total_props += t
        total_pls   += p

    # ── Aprovadas (cod 1140) ──────────────────────────────────────────────────
    aprov_data = get(f"{BASE_URL}/proposicoes", {
        "idDeputadoAutor": dep_id,
        "codSituacao": 1140,
        "itens": 100,
    })
    lote_aprov = aprov_data.get("dados", [])
    # Sanity check: se veio 100 na 1ª página, API ignorou o filtro
    aprovadas = 0 if len(lote_aprov) == 100 else len(lote_aprov)

    # ── Discursos ─────────────────────────────────────────────────────────────
    discursos = len(get_all_pages(
        f"{BASE_URL}/deputados/{dep_id}/discursos",
        {
            "dataInicio": MANDATO_INICIO,
            "dataFim":    datetime.date.today().isoformat(),
        },
        max_pages=20,
    ))

    # ── Presença em eventos ───────────────────────────────────────────────────
    total_eventos  = 0
    total_presente = 0
    for ano in MANDATO_ANOS:
        eventos = get_all_pages(
            f"{BASE_URL}/deputados/{dep_id}/eventos",
            {"dataInicio": f"{ano}-01-01", "dataFim": f"{ano}-12-31"},
            max_pages=10,
        )
        total_eventos += len(eventos)
        total_presente += sum(
            1 for e in eventos
            if (e.get("frequenciaDeputado") or "").upper() in ("P", "")
        )

    # ── Despesas CEAP ─────────────────────────────────────────────────────────
    gasto_total   = 0.0
    despesas_meses = {}
    for ano in MANDATO_ANOS:
        for mes in range(1, 13):
            d = get(f"{BASE_URL}/deputados/{dep_id}/despesas", {
                "ano": ano, "mes": mes, "itens": 200,
            })
            lote = d.get("dados", [])
            if lote:
                gasto = sum(item.get("valorDocumento", 0) or 0 for item in lote)
                gasto_total += gasto
                despesas_meses[f"{ano}-{str(mes).zfill(2)}"] = round(gasto, 2)

    # ── Dias úteis ────────────────────────────────────────────────────────────
    from datetime import date, timedelta
    def dias_uteis(inicio, fim):
        count = 0
        d = inicio
        while d <= fim:
            if d.weekday() < 5:  # segunda=0 a sexta=4
                count += 1
            d += timedelta(days=1)
        return count

    inicio = date(2023, 2, 1)
    hoje   = date.today()
    fim    = min(hoje, date(2027, 1, 31))
    dias_uteis_total = dias_uteis(inicio, fim)

    # Detectar afastamentos
    dias_afastado = 0
    alerta = ""
    for m in (mandatos.get("dados") or []):
        if m.get("situacaoNaData") and any(
            p in (m["situacaoNaData"] or "").lower()
            for p in ["licen", "afasta", "suspens"]
        ):
            ini_m = date.fromisoformat(m["dataInicio"]) if m.get("dataInicio") else inicio
            fim_m = date.fromisoformat(m["dataFim"])    if m.get("dataFim")    else fim
            dias_afastado += dias_uteis(ini_m, fim_m)
            alerta = m["situacaoNaData"]

    return {
        "id":              dep_id,
        "detalhes":        detalhes.get("dados", {}),
        "dias_uteis":      dias_uteis_total,
        "dias_afastado":   dias_afastado,
        "alerta_afastamento": alerta,
        "proposicoes": {
            "total":      total_props,
            "pls":        total_pls,
            "por_ano":    props_por_ano,
        },
        "aprovadas":       aprovadas,
        "discursos":       discursos,
        "presenca": {
            "total_eventos": total_eventos,
            "presente":      total_presente,
        },
        "gasto_total":     round(gasto_total, 2),
        "despesas_meses":  despesas_meses,
        "atualizado_em":   datetime.datetime.utcnow().isoformat() + "Z",
    }


# ─── Coleta: Resumo de Despesas ───────────────────────────────────────────────
def coletar_despesas_summary(deputados: list) -> list:
    """Top 20 deputados por gasto CEAP no ano atual."""
    log.info("Coletando resumo de despesas...")
    ano = datetime.date.today().year
    gastos = []

    for dep in deputados[:30]:  # amostra dos primeiros para não demorar demais
        dep_id = dep["id"]
        total = 0.0
        for mes in range(1, 13):
            d = get(f"{BASE_URL}/deputados/{dep_id}/despesas", {
                "ano": ano, "mes": mes, "itens": 200,
            })
            for item in d.get("dados", []):
                total += item.get("valorDocumento", 0) or 0

        gastos.append({
            "id":      dep_id,
            "nome":    dep.get("nome"),
            "partido": dep.get("siglaPartido"),
            "uf":      dep.get("siglaUf"),
            "gasto":   round(total, 2),
        })
        time.sleep(0.2)

    return sorted(gastos, key=lambda x: x["gasto"], reverse=True)[:20]


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Coletor de dados do Painel Político BR")
    parser.add_argument("--lista",   action="store_true", help="Coleta apenas a lista de deputados")
    parser.add_argument("--detalhe", type=int,            help="Coleta detalhe de um deputado específico")
    parser.add_argument("--todos",   action="store_true", help="Coleta detalhe de TODOS os deputados (lento)")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    DETALHE_DIR.mkdir(exist_ok=True)

    agora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
    timestamp = agora.strftime("%d/%m/%Y às %H:%M") + " (Horário de Brasília)"

    # ── Modo: detalhe único ───────────────────────────────────────────────────
    if args.detalhe:
        detalhe = coletar_detalhe_deputado(args.detalhe)
        save(DETALHE_DIR / f"{args.detalhe}.json", detalhe)
        return

    # ── Coleta base (sempre) ──────────────────────────────────────────────────
    log.info("=== Iniciando coleta de dados ===")

    deputados  = coletar_deputados()
    partidos   = coletar_partidos()
    votacoes   = coletar_votacoes()
    proposicoes = coletar_proposicoes()
    senadores  = coletar_senadores()

    # Mapa de partidos por nº de cadeiras
    partido_map = {}
    for d in deputados:
        s = d.get("siglaPartido") or "SEM PARTIDO"
        partido_map[s] = partido_map.get(s, 0) + 1

    save(DATA_DIR / "deputados.json",    {"dados": deputados, "partidoMap": partido_map})
    save(DATA_DIR / "partidos.json",     {"dados": partidos})
    save(DATA_DIR / "votacoes.json",     {"dados": votacoes})
    save(DATA_DIR / "proposicoes.json",  {"dados": proposicoes})
    save(DATA_DIR / "senadores.json",    {"dados": senadores})

    if args.lista:
        save(DATA_DIR / "meta.json", {"atualizado_em": timestamp, "modo": "lista"})
        log.info("=== Coleta (lista) concluída ===")
        return

    # ── Detalhes de todos os deputados ───────────────────────────────────────
    if args.todos:
        log.info(f"Coletando detalhe de {len(deputados)} deputados...")
        for i, dep in enumerate(deputados, 1):
            dep_id = dep["id"]
            path   = DETALHE_DIR / f"{dep_id}.json"

            # Pula se já foi coletado hoje
            if path.exists():
                mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
                if mtime.date() == datetime.date.today():
                    log.info(f"  [{i}/{len(deputados)}] {dep['nome']} — já atualizado hoje")
                    continue

            log.info(f"  [{i}/{len(deputados)}] {dep['nome']}")
            try:
                detalhe = coletar_detalhe_deputado(dep_id)
                save(path, detalhe)
            except Exception as e:
                log.error(f"    Erro: {e}")

            # Pausa a cada 10 deputados para não sobrecarregar a API
            if i % 10 == 0:
                log.info("  Pausa de 5s...")
                time.sleep(5)

    save(DATA_DIR / "meta.json", {
        "atualizado_em":   timestamp,
        "total_deputados": len(deputados),
        "total_partidos":  len(partidos),
        "total_senadores": len(senadores),
        "modo":            "completo" if args.todos else "base",
    })

    log.info("=== Coleta concluída ===")


if __name__ == "__main__":
    main()
