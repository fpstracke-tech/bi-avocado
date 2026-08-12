#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETL do Resumo Executivo CIRAD / Tropisens  →  Supabase (cirad_resumo)

Substitui o `Tropisens.py`, que rodava na máquina do Phil com poppler e
tesseract em caminho absoluto do Windows. Três diferenças que mudam o
resultado, não só o lugar de execução:

1. A SEMANA SAI DO PDF.
   O `Tropisens.py` montava o título com `date.today().isocalendar()`. Rodar
   com um dia de atraso já fazia o resumo anunciar uma semana que o boletim
   não tem — e o prompt pedia "não inventar semanas" enquanto a semana
   injetada era outra. Aqui a semana vem do documento e é conferida contra
   `europa_cirad_precos`: se o preço daquela semana não está no banco, o
   resumo não é gerado. Assim o resumo nunca fala de uma semana que a aba de
   preços não mostra.

2. TEXTO NATIVO, NÃO OCR.
   O boletim tem camada de texto. O `Tropisens.py` convertia página em imagem
   a 200 dpi e passava tesseract, então os números que chegavam ao modelo já
   eram leitura de pixel. Aqui sai do pdfplumber.

3. OS NÚMEROS VÊM DO BANCO.
   Preço de referência, variação e grade por calibre são lidos de
   `europa_cirad_precos` e `europa_cirad_calibre` — as mesmas linhas que a aba
   de preços mostra — e entram no prompt como fato dado. O modelo escreve a
   prosa em volta. Depois de gerar, TODA cifra em euro do resumo é conferida
   contra essa lista: aparecendo um valor que não está no banco, nada é
   gravado e o job falha. É o que impede o resumo de discordar do dashboard.

Uso:
    python etl_cirad_resumo.py                    # PDF mais novo do ownCloud
    python etl_cirad_resumo.py --arquivo x.pdf
    python etl_cirad_resumo.py --pasta ./PDF      # o mais novo da pasta
    python etl_cirad_resumo.py --dry-run          # mostra, não grava
    python etl_cirad_resumo.py --forcar           # refaz semana já existente
    python etl_cirad_resumo.py --sem-modelo       # só o bloco de números

Ambiente:
    SUPABASE_URL, SUPABASE_KEY      obrigatórios (KEY = chave secreta)
    ANTHROPIC_API_KEY               obrigatório, salvo com --sem-modelo
    ANTHROPIC_MODELO                opcional, default claude-sonnet-5
    OWNCLOUD_URL / OWNCLOUD_PASTA / OWNCLOUD_USER / OWNCLOUD_PASS
                                    só quando não se passa --arquivo/--pasta
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import pdfplumber
except ImportError:                                    # pragma: no cover
    print("ERRO: falta o pdfplumber (pip install pdfplumber).")
    raise

# reaproveita o extrator do boletim: mesma detecção de semana/ano e o mesmo
# acesso WebDAV. Duas cópias dessas regras divergiriam na primeira mudança de
# layout, e aí resumo e preço passariam a falar de semanas diferentes.
import etl_europa_cirad as boletim
from supabase_upsert import upsert

TABELA = "cirad_resumo"
CHAVE  = "ano,semana,secao"

# Claude Sonnet 5: o próprio doc da Anthropic o indica para escrita e resumo.
# O Tropisens.py usava gpt-4.1 porque foi escrito antes de a casa ter Claude —
# não havia razão técnica para manter uma segunda conta de API viva só por isso.
MODELO_PADRAO = "claude-sonnet-5"

# As cinco seções do resumo, na ordem, iguais às que já estão no banco desde a
# carga do Tropisens. Sem emoji: o dashboard não usa emoji, e o nome da seção é
# chave de upsert — um emoji a mais criaria linha nova em vez de atualizar.
SECOES = [
    "Panorama Geral (Europa)",
    "Preços – Mercado Europeu (€/caixa 4kg)",
    "Origens – Destaques",
    "Estados Unidos",
    "Leitura para o Produtor Brasileiro",
]

METODO = ("texto nativo do PDF (pdfplumber) + números de europa_cirad_precos "
          "e europa_cirad_calibre; prosa por modelo de linguagem; cifras "
          "conferidas contra o banco antes de gravar")

SISTEMA = ("Você é um analista sênior do mercado global de abacate Hass, "
           "escrevendo para produtores brasileiros.")

# Cifra em euro em qualquer das formas que o modelo pode escrever:
# "€10,50", "10,50 €", "€ 10.50", "10,50/kg"
RE_EURO = re.compile(r"(?:€\s*([\d]{1,3}[.,]\d{1,2})|([\d]{1,3}[.,]\d{1,2})\s*€)")
TOLERANCIA = 0.02


# ── SUPABASE (leitura) ────────────────────────────────────────────────────
def _cfg():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL e SUPABASE_KEY são obrigatórios.")
    return url, key


def sb_get(tabela: str, params: dict) -> list[dict]:
    url, key = _cfg()
    r = requests.get(f"{url}/rest/v1/{tabela}", params=params, timeout=90,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()


# ── NÚMEROS OFICIAIS ──────────────────────────────────────────────────────
def numeros_do_banco(ano: int, semana: int) -> dict:
    """
    O que a aba de preços mostra para esta semana. É a única fonte de número
    que o modelo vai receber.
    """
    grades = sb_get("europa_cirad_precos", {
        "select": "grade,preco_eur,variacao_eur,variacao_media_pct,unidade,relatorio_semana",
        "ano": f"eq.{ano}", "semana": f"eq.{semana}", "order": "grade.asc"})
    hist = sb_get("v_europa_cirad_semanal", {
        "select": "ano,semana,preco", "grade": "eq.Hass 18",
        "ano": f"eq.{ano}", "semana": f"lte.{semana}",
        "order": "semana.desc", "limit": "6"})
    calibre = sb_get("europa_cirad_calibre", {
        "select": "variedade,origem,calibre,preco_min,preco_max,unidade",
        "ano": f"eq.{ano}", "semana": f"eq.{semana}",
        "order": "variedade.asc,origem.asc,calibre.asc"})
    # linha sem preco nenhum e artefato de cabecalho da grade; no prompt ela
    # so ocupa espaco e sugere que existe cotacao onde nao existe
    calibre = [c for c in calibre
               if c.get("preco_min") is not None or c.get("preco_max") is not None]
    return {"ano": ano, "semana": semana, "grades": grades,
            "historico": list(reversed(hist)), "calibre": calibre}


def valores_permitidos(dados: dict) -> set[float]:
    """Todo valor em euro que o resumo pode citar."""
    ok: set[float] = set()
    for g in dados["grades"]:
        for c in ("preco_eur", "variacao_eur"):
            if g.get(c) is not None:
                ok.add(round(abs(float(g[c])), 2))
    for h in dados["historico"]:
        if h.get("preco") is not None:
            ok.add(round(float(h["preco"]), 2))
    for c in dados["calibre"]:
        for k in ("preco_min", "preco_max"):
            if c.get(k) is not None:
                ok.add(round(float(c[k]), 2))
    return ok


def bloco_numeros(dados: dict) -> str:
    """O bloco que entra no prompt como fato dado."""
    L = [f"NÚMEROS OFICIAIS — semana {dados['semana']}/{dados['ano']}",
         "(vindos do banco do projeto, já conferidos; use SOMENTE estes)",
         "", "Preço de referência por grade:"]
    for g in dados["grades"]:
        p = f"  {g['grade']}: {float(g['preco_eur']):.2f} {g['unidade']}"
        if g.get("variacao_eur") is not None:
            p += f", variação de {float(g['variacao_eur']):+.2f} euro sobre a semana anterior"
        if g.get("variacao_media_pct") is not None:
            p += f", {float(g['variacao_media_pct']):+.0f}% sobre a média das duas safras anteriores"
        L.append(p)

    if dados["historico"]:
        L += ["", "Hass 18, semanas recentes (EUR/caixa 4kg):"]
        L += [f"  S{h['semana']}/{h['ano']}: {float(h['preco']):.2f}"
              for h in dados["historico"]]

    if dados["calibre"]:
        L += ["", "Cotação por origem e calibre:"]
        for c in dados["calibre"]:
            faixa = ("/".join(f"{float(c[k]):.2f}" for k in ("preco_min", "preco_max")
                              if c.get(k) is not None)) or "—"
            origem = c["origem"] or "sem origem declarada"
            L.append(f"  {c['variedade']} {origem} calibre {c['calibre']}: "
                     f"{faixa} {c['unidade']}")
    return "\n".join(L)


def instrucoes(ano: int, semana: int, rel_semana: int | None) -> str:
    nota = ""
    if rel_semana and rel_semana != semana:
        nota = (f"\nO boletim é a edição da semana {rel_semana}, mas o preço que "
                f"ele publica é o da semana {semana}. Fale da semana {semana}.")
    return f"""Escreva um resumo CURTO, DIRETO e OBJETIVO do mercado de abacate Hass,
no formato de mensagem de WhatsApp para PRODUTORES BRASILEIROS.

Semana de referência: {semana:02d} – {ano}.{nota}

Devolva EXATAMENTE cinco seções, cada uma começando com "## " e o título
literal abaixo, na ordem, sem inventar nem renomear seção:

## {SECOES[0]}
## {SECOES[1]}
## {SECOES[2]}
## {SECOES[3]}
## {SECOES[4]}

Regras:
- Português do Brasil, frases curtas.
- NÃO use emojis. NÃO use tabelas. NÃO escreva título antes da primeira seção.
- Todo preço em euro que você citar tem que estar no bloco NÚMEROS OFICIAIS,
  com o mesmo valor. Não arredonde, não converta, não calcule preço novo.
- Volume, participação de origem, qualidade e demanda saem do texto do
  boletim; preço sai só dos NÚMEROS OFICIAIS.
- Se o boletim não falar dos Estados Unidos, escreva na seção que a edição
  desta semana não trouxe leitura do mercado americano.
- A última seção é a conclusão prática para quem produz no Brasil: o que
  fazer, olhando calibre, janela e origem concorrente."""


# ── MODELO ────────────────────────────────────────────────────────────────
def gera_resumo(texto_pdf: str, dados: dict, rel_semana: int | None,
                modelo: str) -> str:
    from anthropic import Anthropic                      # import tardio
    chave = os.environ.get("ANTHROPIC_API_KEY", "")
    if not chave:
        raise EnvironmentError("ANTHROPIC_API_KEY não configurada.")
    cliente = Anthropic(api_key=chave)
    # max_tokens é obrigatório na Messages API. O resumo tem cinco seções
    # curtas; 2000 dá folga de sobra e ainda corta uma resposta que fugisse do
    # formato pedido em vez de gerar página.
    r = cliente.messages.create(
        model=modelo,
        max_tokens=2000,
        system=SISTEMA,
        messages=[{"role": "user", "content": "\n\n".join([
            instrucoes(dados["ano"], dados["semana"], rel_semana),
            bloco_numeros(dados),
            "TEXTO DO BOLETIM:\n\n" + texto_pdf,
        ])}])
    partes = [b.text for b in r.content if getattr(b, "type", "") == "text"]
    if r.stop_reason == "max_tokens":
        print("  AVISO: a resposta foi cortada no limite de tokens — o formato "
              "de cinco seções provavelmente ficou incompleto.")
    return "\n".join(partes).strip()


def secoes_do_texto(saida: str) -> list[tuple[int, str, str]]:
    """[(ordem, secao, texto)] a partir dos cabeçalhos '## '."""
    partes = re.split(r"^\s*##\s*", saida, flags=re.M)
    achados: dict[str, str] = {}
    for p in partes:
        if not p.strip():
            continue
        linha, _, corpo = p.partition("\n")
        titulo = linha.strip().strip("#").strip()
        for s in SECOES:
            # compara sem acento nem pontuação: o modelo troca – por - com
            # frequência, e isso não deveria criar seção nova
            def chave(x):
                return re.sub(r"[^a-z0-9]", "", x.lower()
                              .replace("ç", "c").replace("ã", "a").replace("é", "e")
                              .replace("í", "i").replace("ó", "o").replace("ú", "u")
                              .replace("â", "a").replace("ê", "e").replace("õ", "o"))
            if chave(titulo) == chave(s):
                achados[s] = corpo.strip()
                break
    return [(i + 1, s, achados[s]) for i, s in enumerate(SECOES) if achados.get(s)]


# ── CONFERÊNCIA DAS CIFRAS ────────────────────────────────────────────────
def confere_cifras(secoes: list[tuple[int, str, str]],
                   permitidos: set[float]) -> list[str]:
    """Devolve a lista de cifras em euro que NÃO existem no banco."""
    fora = []
    for _, secao, texto in secoes:
        for m in RE_EURO.finditer(texto):
            bruto = m.group(1) or m.group(2)
            try:
                v = round(abs(float(bruto.replace(".", "").replace(",", "."))), 2)
            except ValueError:
                continue
            if not any(abs(v - p) <= TOLERANCIA for p in permitidos):
                fora.append(f"{bruto} (em '{secao}')")
    return fora


# ── PDF ───────────────────────────────────────────────────────────────────
def le_pdf(fonte, rotulo: str) -> tuple[str, int | None, int | None]:
    """(texto completo, semana do relatório, ano do relatório)."""
    with pdfplumber.open(fonte) as pdf:
        paginas = [(p.extract_text() or "") for p in pdf.pages]
    texto = "\n\n".join(paginas)
    sem = ano = None
    if paginas:
        sem, ano = boletim.semana_ano(paginas[0], rotulo, rotulo)
    return texto, sem, ano


def pdf_mais_novo(argv) -> tuple[str, object]:
    if "--arquivo" in argv:
        p = Path(argv[argv.index("--arquivo") + 1])
        if not p.exists():
            raise FileNotFoundError(f"{p} não existe")
        return p.name, str(p)
    if "--pasta" in argv:
        d = Path(argv[argv.index("--pasta") + 1])
        pdfs = sorted(d.rglob("*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not pdfs:
            raise FileNotFoundError(f"nenhum PDF em {d}")
        print(f"  pasta {d}: {len(pdfs)} PDFs, o mais novo é {pdfs[0].name}")
        return pdfs[0].name, str(pdfs[0])
    base, auth, achados = boletim.webdav_listar_pdfs()
    if not achados:
        raise FileNotFoundError("nenhum PDF na pasta do ownCloud")
    return boletim.webdav_baixar(base, auth, achados[0][0])


# ── MAIN ──────────────────────────────────────────────────────────────────
def main() -> int:
    argv = sys.argv
    dry = "--dry-run" in argv
    forcar = "--forcar" in argv
    sem_modelo = "--sem-modelo" in argv
    # `or` em vez do default do .get: no GitHub Actions, `${{ vars.X }}` de uma
    # variável que não existe chega como string VAZIA, e aí a chave existe no
    # ambiente — o default do .get nunca entra e a API recebe model="".
    modelo = (os.environ.get("ANTHROPIC_MODELO") or "").strip() or MODELO_PADRAO

    print(f"ETL Resumo CIRAD · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    nome, fonte = pdf_mais_novo(argv)
    texto, sem_rel, ano_rel = le_pdf(fonte, nome)
    print(f"  PDF: {nome} · {len(texto)} caracteres de texto nativo")
    if not sem_rel or not ano_rel:
        print("  ERRO: não achei semana/ano no documento. Sem isso o resumo "
              "sairia com semana inventada — era o defeito do Tropisens.py.")
        return 1
    print(f"  edição: semana {sem_rel}/{ano_rel}")
    if len(texto) < 2000:
        print(f"  ERRO: só {len(texto)} caracteres de texto. O boletim tem "
              "camada de texto; tão pouco assim indica PDF digitalizado ou "
              "arquivo truncado. Abortando em vez de resumir o vazio.")
        return 1

    # A semana do PREÇO é a que o etl_europa_cirad gravou para esta edição.
    # Perguntar ao banco em vez de recalcular mantém as duas telas coerentes.
    achou = sb_get("europa_cirad_precos", {
        "select": "ano,semana,relatorio_semana", "grade": "eq.Hass 18",
        "relatorio_ano": f"eq.{ano_rel}", "relatorio_semana": f"eq.{sem_rel}",
        "order": "semana.desc", "limit": "1"})
    if not achou:
        print(f"  ERRO: europa_cirad_precos não tem preço da edição "
              f"{sem_rel}/{ano_rel}.\n"
              f"  Rode o etl_europa_cirad.py neste PDF primeiro: o resumo cita "
              f"preço,\n  e preço que não está no banco não pode ser citado.")
        return 1
    ano, semana = int(achou[0]["ano"]), int(achou[0]["semana"])
    if semana != sem_rel:
        print(f"  preço publicado é o da semana {semana}/{ano} "
              f"(edição {sem_rel}) — o resumo fala da {semana}")

    if not forcar:
        ja = sb_get(TABELA, {"select": "secao", "ano": f"eq.{ano}",
                             "semana": f"eq.{semana}", "limit": "1"})
        if ja:
            print(f"  semana {semana}/{ano} já está em {TABELA}. "
                  f"Nada a fazer (use --forcar para refazer).")
            return 0

    dados = numeros_do_banco(ano, semana)
    permitidos = valores_permitidos(dados)
    print(f"  números do banco: {len(dados['grades'])} grades, "
          f"{len(dados['calibre'])} linhas de calibre, "
          f"{len(permitidos)} cifras permitidas")
    if not dados["grades"]:
        print("  ERRO: nenhuma grade no banco para esta semana.")
        return 1

    if sem_modelo:
        print("\n" + bloco_numeros(dados))
        print("\n  --sem-modelo: nada foi gerado nem gravado.")
        return 0

    print(f"  chamando {modelo}...", flush=True)
    saida = gera_resumo(texto, dados, sem_rel, modelo)
    if not saida:
        print("  ERRO: o modelo devolveu vazio.")
        return 1

    secoes = secoes_do_texto(saida)
    print(f"  {len(secoes)}/{len(SECOES)} seções reconhecidas")
    faltando = [s for s in SECOES if s not in [x[1] for x in secoes]]
    if faltando:
        print(f"  AVISO: seção sem texto: {faltando}")
    if len(secoes) < 3:
        print("  ERRO: menos de 3 seções reconhecidas — a saída não seguiu o "
              "formato pedido. Não gravo resumo picado.")
        print("  --- saída do modelo ---")
        print(saida[:1500])
        return 1

    fora = confere_cifras(secoes, permitidos)
    if fora:
        print("\n  ERRO: o resumo cita cifra em euro que não está no banco:")
        for f in fora:
            print(f"    {f}")
        print("\n  Cifras aceitas nesta semana: "
              + ", ".join(f"{v:.2f}" for v in sorted(permitidos)))
        print("  Nada foi gravado. Rodar de novo costuma resolver; se repetir, "
              "o prompt\n  ou o bloco de números é que está errado.")
        return 1
    print("  conferência de cifras: todas batem com o banco")

    agora = datetime.now(timezone.utc).isoformat()
    regs = [{"ano": ano, "semana": semana, "ordem": o, "secao": s, "texto": t,
             "arquivo": nome, "relatorio_semana": sem_rel, "modelo": modelo,
             "metodo": METODO, "conferido": True, "extracted_at": agora}
            for o, s, t in secoes]

    print(f"\n  Resumo · semana {semana}/{ano}")
    for o, s, t in secoes:
        print(f"\n  [{o}] {s}")
        for linha in t.split("\n"):
            print(f"      {linha}")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 0

    upsert(TABELA, regs, on_conflict=CHAVE)
    print(f"\n  OK {TABELA}: {len(regs)} seções da semana {semana}/{ano}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
