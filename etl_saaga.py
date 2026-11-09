#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oferta de abacate na Europa (UE + Reino Unido) por origem, semana a semana.

Fonte: SAAGA, South African Avocado Growers' Association, página Export Market
(https://avocado.co.za/export-market/). Toda semana sai um Excel
"EU-UK-SUPPLY-<ano>-Week-<nn>.xlsx" com as chegadas no mercado europeu em mil
caixas de 4 kg, por origem, Hass e Green (G/Sk) separados.

O que o Excel tem e como é lido:
  Data <ano>   projeção + realizado da safra corrente. A cor da fonte é o dado:
               PRETO = projeção, COLORIDO = embarque confirmado ("actual").
               Por isso a cor é lida com openpyxl sem data_only e o número
               com data_only (os totais são fórmula).
  Data <aa>    safra anterior inteira, toda realizada.
  Estimates vs Actulas (sic)
               a estimativa ORIGINAL do começo do ano ao lado do realizado,
               por origem. Guardada em `estimativa_inicial` para medir o erro
               da projeção.

A semana é a semana NO MERCADO (chegada na Europa), não a de embarque. O
próprio Excel lista o deslocamento usado por origem (Peru e Chile: embarque S1
= mercado S5; Brasil: S1 = S4).

Uso:
    python etl_saaga.py                  # baixa o Excel mais novo e grava
    python etl_saaga.py --dry-run        # só mostra
    python etl_saaga.py --arquivo X.xlsx # lê arquivo local
    python etl_saaga.py --json saida.json
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone

import requests

try:
    import openpyxl
except ImportError:                                    # pragma: no cover
    print("ERRO: falta o openpyxl (pip install openpyxl)")
    raise

PAGINA = "https://avocado.co.za/export-market/"
TABELA = "saaga_oferta_europa"
CHAVE = "ano,semana,origem,variedade"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")}

# nomes do Excel -> pt-BR (o painel inteiro é em português)
ORIGENS = {
    "israel": "Israel", "spain": "Espanha", "mexico": "México",
    "kenya & tanzania": "Quênia e Tanzânia", "kenya & tanzaina": "Quênia e Tanzânia",
    "kenya & tanzania est": "Quênia e Tanzânia", "argentina": "Argentina", "peru": "Peru",
    "chile": "Chile", "south africa": "África do Sul", "colombia": "Colômbia",
    "brazil": "Brasil", "morocco": "Marrocos",
}
PRETOS = {None, "FF000000", "00000000", "000000"}


def origem_pt(rotulo: str | None) -> str | None:
    if not rotulo:
        return None
    r = re.sub(r"\s+(estimate|est|actual)$", "", str(rotulo).strip().lower())
    return ORIGENS.get(r)


def achar_xlsx() -> str:
    """URL do Excel mais recente, lido dos href da página (o nome muda: '-update-1')."""
    html = requests.get(PAGINA, headers=HEADERS, timeout=60).text
    urls = set(re.findall(r'https?://[^"\'\s]+EU-UK-SUPPLY[^"\'\s]*\.xlsx', html, re.I))
    if not urls:
        raise RuntimeError("nenhum Excel EU-UK-SUPPLY na página da SAAGA")

    def chave(u: str):
        a = re.search(r"SUPPLY-(\d{4})", u, re.I)
        w = re.search(r"Week-(\d+)", u, re.I)
        upd = re.search(r"update-(\d+)", u, re.I)
        return (int(a.group(1)) if a else 0, int(w.group(1)) if w else 0,
                int(upd.group(1)) if upd else 0)
    return max(urls, key=chave)


def grupos_de_colunas(ws, linha_cab: int) -> list[tuple[int, str]]:
    """[(coluna G/Sk, origem)]. O rótulo da origem fica na linha de cima, às vezes
    sobre a coluna G/Sk, às vezes centralizado sobre a Hass — por isso a janela."""
    saida = []
    for c in range(1, ws.max_column + 1):
        if str(ws.cell(linha_cab, c).value or "").strip().lower() != "g/sk":
            continue
        for cc in (c, c + 1, c - 1, c + 2):
            o = origem_pt(ws.cell(linha_cab - 1, cc).value) if cc >= 1 else None
            if o:
                saida.append((c, o))
                break
    return saida


def linha_cabecalho(ws) -> int:
    for r in range(1, 12):
        if any(str(ws.cell(r, c).value or "").strip().lower() == "g/sk" for c in range(1, 12)):
            return r
    raise RuntimeError(f"aba {ws.title}: não achei a linha G/Sk / Hass / Total")


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3)


def ler_ano(wv, wf, aba: str, ano: int, col_semana: int, so_realizado: bool) -> dict:
    wsv, wsf = wv[aba], wf[aba]
    cab = linha_cabecalho(wsv)
    grupos = grupos_de_colunas(wsv, cab)
    if len(grupos) < 8:
        raise RuntimeError(f"aba {aba}: só {len(grupos)} origens reconhecidas")
    saida = {}
    for r in range(cab + 1, wsv.max_row + 1):
        sem = wsv.cell(r, col_semana).value
        if not isinstance(sem, (int, float)) or not 1 <= int(sem) <= 53:
            continue
        sem = int(sem)
        for c, origem in grupos:
            for var, cc in (("Green", c), ("Hass", c + 1)):
                v = num(wsv.cell(r, cc).value)
                if v is None:
                    continue
                cor = wsf.cell(r, cc).font.color
                rgb = cor.rgb if (cor is not None and cor.type == "rgb") else None
                real = True if so_realizado else (rgb not in PRETOS)
                saida[(ano, sem, origem, var)] = {
                    "ano": ano, "semana": sem, "origem": origem, "variedade": var,
                    "caixas_mil": v, "status": "realizado" if real else "projetado"}
        if sem == 52 and col_semana == 1:
            break
    return saida


def ler_estimativa(wv, saida: dict, ano: int) -> int:
    aba = next((n for n in wv.sheetnames if n.lower().startswith("estimates vs")), None)
    if not aba:
        return 0
    ws = wv[aba]
    cab = linha_cabecalho(ws)
    est = []
    for c in range(1, ws.max_column + 1):
        if str(ws.cell(cab, c).value or "").strip().lower() != "g/sk":
            continue
        for cc in (c, c + 1, c - 1):
            rot = str(ws.cell(cab - 1, cc).value or "").lower() if cc >= 1 else ""
            if "estimate" in rot or rot.endswith(" est"):
                o = origem_pt(rot)
                if o:
                    est.append((c, o))
                break
    n = 0
    for r in range(cab + 1, ws.max_row + 1):
        sem = ws.cell(r, 1).value
        if not isinstance(sem, (int, float)) or not 1 <= int(sem) <= 53:
            continue
        for c, origem in est:
            for var, cc in (("Green", c), ("Hass", c + 1)):
                v = num(ws.cell(r, cc).value)
                k = (ano, int(sem), origem, var)
                if v is None:
                    continue
                if k not in saida:
                    saida[k] = {"ano": ano, "semana": int(sem), "origem": origem, "variedade": var,
                                "caixas_mil": None, "status": "projetado"}
                saida[k]["estimativa_inicial"] = v
                n += 1
    return n


def extrair(conteudo: bytes, nome: str) -> tuple[list[dict], dict]:
    wv = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True)
    wf = openpyxl.load_workbook(io.BytesIO(conteudo))
    aba_atual = next(n for n in wv.sheetnames if re.fullmatch(r"Data \d{4}", n.strip()))
    ano = int(aba_atual.split()[-1])
    titulo = " ".join(str(c.value) for row in wv[aba_atual].iter_rows(max_row=2) for c in row if c.value)
    m = re.search(r"updated\s+(\d{1,2})/(\d{1,2})/(\d{4})", titulo, re.I)
    atualizado = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None
    w = re.search(r"Week-(\d+)", nome, re.I)

    saida = ler_ano(wv, wf, aba_atual, ano, col_semana=1, so_realizado=False)
    n_est = ler_estimativa(wv, saida, ano)

    aba_ant = next((n for n in wv.sheetnames if re.fullmatch(rf"Data {str(ano - 1)[2:]}", n.strip())), None)
    if aba_ant:
        # a planilha do ano anterior traz 3 colunas de semana: SA PACK, SA SHIP e
        # On Market. Vale a de mercado (3a), igual à aba do ano corrente.
        saida.update(ler_ano(wv, wf, aba_ant, ano - 1, col_semana=3, so_realizado=True))

    meta = {"arquivo": nome, "edicao_semana": int(w.group(1)) if w else None,
            "atualizado_em": atualizado, "ano": ano, "estimativas": n_est}
    agora = datetime.now(timezone.utc).isoformat()
    linhas = []
    for r in saida.values():
        r.setdefault("estimativa_inicial", None)
        r.update({"arquivo": nome, "edicao_semana": meta["edicao_semana"],
                  "atualizado_em": atualizado, "extracted_at": agora})
        linhas.append(r)
    linhas.sort(key=lambda r: (r["ano"], r["semana"], r["origem"], r["variedade"]))
    return linhas, meta


def resumo(linhas: list[dict], meta: dict) -> None:
    ano = meta["ano"]
    hass = [r for r in linhas if r["ano"] == ano and r["variedade"] == "Hass" and r["caixas_mil"]]
    ult_real = max((r["semana"] for r in hass if r["status"] == "realizado"), default=None)
    tot = sum(r["caixas_mil"] for r in hass)
    print(f"SAAGA {meta['arquivo']} (atualizado {meta['atualizado_em']}): {len(linhas)} linhas")
    print(f"  Hass {ano}: {tot:,.0f} mil caixas no ano; realizado até S{ult_real}")
    for o in ("Peru", "Chile", "Brasil", "Colômbia"):
        s = [r for r in hass if r["origem"] == o and r["semana"] == ult_real]
        if s:
            print(f"  {o:10s} S{ult_real}: {s[0]['caixas_mil']:,.1f} mil cx ({s[0]['status']})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--arquivo")
    ap.add_argument("--json")
    a = ap.parse_args()

    if a.arquivo:
        with open(a.arquivo, "rb") as f:
            conteudo, nome = f.read(), a.arquivo.replace("\\", "/").split("/")[-1]
    else:
        url = achar_xlsx()
        print("baixando", url)
        r = requests.get(url, headers=HEADERS, timeout=120)
        r.raise_for_status()
        conteudo, nome = r.content, url.split("/")[-1]

    linhas, meta = extrair(conteudo, nome)
    resumo(linhas, meta)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "linhas": linhas}, f, ensure_ascii=False)
    if a.dry_run:
        return 0
    import supabase_upsert
    supabase_upsert.upsert(TABELA, linhas, on_conflict=CHAVE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
