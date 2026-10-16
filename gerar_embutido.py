#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_embutido.py — regrava o fallback embutido do dashboard a partir do banco
=============================================================================
BI Avocado — TFruits

O `index.html` lê o Supabase ao vivo, bloco por bloco, e cai no dado EMBUTIDO
(`const DATA` / `const SNAP.cirad`) quando uma view não responde. Esse embutido
era escrito à mão: em 18/08/2026 ele estava com a semana 31 do CIRAD enquanto o
banco já tinha a 33. Fallback velho é pior que fallback ausente, porque a tela
não dá erro — ela mostra número antigo com cara de número atual.

Este script fecha o ciclo: lê as mesmas views que o front lê e regrava o
embutido, para o dashboard nunca cair mais de um dia atrás do banco.

REGRA CENTRAL: a transformação aqui é a MESMA das funções `carrega*` do
`index.html`. Se as duas divergirem, o dashboard passa a mostrar uma coisa ao
vivo e outra no fallback — e ninguém percebe, porque só um dos dois caminhos
roda por vez. Por isso cada função abaixo diz de qual função JS é o espelho.

O merge das origens é semana a semana, de propósito: Chile 2023-2025 e a
projeção da Argentina vêm de planilha e NÃO estão no banco. Substituir o ano
inteiro apagaria esses anos do fallback.

Uso:
    python gerar_embutido.py                     # index.html + bi_avocado.html + .json
    python gerar_embutido.py --dry-run           # mostra o que mudaria
    python gerar_embutido.py --arquivo out.html  # um arquivo específico

Ambiente:
    SUPABASE_URL                     obrigatório
    SUPABASE_ANON | SUPABASE_KEY     obrigatório (basta permissão de leitura)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ALVOS_HTML = ["index.html", "bi_avocado.html"]
ALVO_JSON = "bi_avocado_data.json"

# json compacto, igual ao que já está no arquivo: separador sem espaço
SEP = (",", ":")

PAR_DE_PAIS = {"brasil": "USD/BRL", "chile": "USD/CLP", "colombia": "USD/COP",
               "espanha": "USD/EUR", "israel": "USD/ILS", "marrocos": "USD/MAD",
               "peru": "USD/PEN"}
MAPA_ORIGEM = {"Chile": "chile", "Argentina": "argentina",
               "Uruguai": "uruguai", "Uruguay": "uruguai"}
CATEGORIAS = ("geral", "regulacao", "logistica")


# ── SUPABASE ──────────────────────────────────────────────────────────────
def _cfg():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = (os.environ.get("SUPABASE_ANON") or os.environ.get("SUPABASE_KEY") or "")
    if not url or not key:
        raise EnvironmentError(
            "SUPABASE_URL e SUPABASE_ANON (ou SUPABASE_KEY) são obrigatórios.")
    return url, key


def sb(tabela: str, params: dict) -> list[dict]:
    """Lê uma view/tabela paginando: o PostgREST corta em 1000 por padrão."""
    url, key = _cfg()
    saida, passo, inicio = [], 1000, 0
    while True:
        r = requests.get(f"{url}/rest/v1/{tabela}", params=params, timeout=90,
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Range-Unit": "items",
                                  "Range": f"{inicio}-{inicio + passo - 1}"})
        r.raise_for_status()
        lote = r.json()
        saida += lote
        if len(lote) < passo:
            return saida
        inicio += passo


def num(v):
    return None if v is None else float(v)


def slug(p: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", (p or "").lower())
    return re.sub(r"[^a-z]", "", s)


# ── BLOCOS (espelho das funções carrega* do index.html) ───────────────────
def bloco_brasil(D) -> int:
    """espelho de carregaBrasil(): série substituída inteira, valor = preco_max"""
    l = sb("v_brasil_precos_semanal",
           {"select": "ano,semana,preco_max,extracted_at", "order": "ano.asc,semana.asc"})
    if not l:
        raise RuntimeError("v_brasil_precos_semanal vazia")
    s = {}
    for r in l:
        s.setdefault(str(r["ano"]), []).append(
            {"semana": int(r["semana"]), "valor": num(r["preco_max"])})
    D["precos"]["brasil"]["series"] = s
    D["precos"]["brasil"]["carga_banco"] = l[-1]["extracted_at"]
    return len(l)


def bloco_origens(D) -> int:
    """
    espelho de carregaOrigens(): merge SEMANA A SEMANA por ano.

    O coletor cobre só parte de um ano (o acervo da UAM começa em out/2024) e
    Chile 2023-2025 / Argentina 2024-2025 vêm de planilha. Trocar o ano inteiro
    apagaria do fallback o que só a planilha tem.
    """
    l = sb("v_precos_origem_semanal",
           {"select": "pais,ano,semana,preco_max,preco_medio,dias,extracted_at",
            "order": "pais.asc,ano.asc,semana.asc"})
    if not l:
        raise RuntimeError("v_precos_origem_semanal vazia")
    porpais, meta = {}, {}
    for r in l:
        k = MAPA_ORIGEM.get(r["pais"])
        if not k:
            continue
        porpais.setdefault(k, {}).setdefault(str(r["ano"]), []).append(
            {"semana": int(r["semana"]), "valor": num(r["preco_max"])})
        m = meta.setdefault(k, {"dias": [], "vies": []})
        if r.get("dias") is not None:
            m["dias"].append(float(r["dias"]))
        if r.get("preco_medio"):
            m["vies"].append((num(r["preco_max"]) - num(r["preco_medio"]))
                             / num(r["preco_medio"]) * 100)

    def media(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    for k, anos in porpais.items():
        if k not in D["precos"]:
            continue
        base = D["precos"][k].get("series") or {}
        saida, mistos = dict(base), []
        for ano, vivo in anos.items():
            antigo = base.get(ano) or []
            if not antigo:
                saida[ano] = vivo
                continue
            porsem = {x["semana"]: x for x in antigo}
            porsem.update({x["semana"]: x for x in vivo})
            saida[ano] = [porsem[s] for s in sorted(porsem)]
            if len(vivo) < len(saida[ano]):
                mistos.append(f"{ano} ({len(vivo)}/{len(saida[ano])})")
        D["precos"][k]["series"] = saida
        D["precos"][k]["live"] = {
            "anos": sorted(anos), "mistos": mistos,
            "dias": media(meta[k]["dias"]), "vies": media(meta[k]["vies"]),
            "vies_max": round(max(meta[k]["vies"]), 4) if meta[k]["vies"] else None,
            "n": len(meta[k]["dias"])}
    return len(l)


def bloco_europa(D) -> int:
    """espelho de carregaEuropa(): série da caixa 4kg, referência e grade por ano"""
    l = sb("v_europa_cirad_semanal",
           {"select": "ano,semana,grade,preco,variacao_eur,variacao_media_pct,"
                      "unidade,relatorio_semana,extracted_at",
            "order": "ano.asc,semana.asc"})
    if not l:
        raise RuntimeError("v_europa_cirad_semanal vazia")
    # a grade 26 é cotada em EUR/kg: no mesmo eixo cairia uma ordem de grandeza
    caixa = [r for r in l if r["unidade"] == "EUR/caixa 4kg"]

    def serie(g):
        s = {}
        for r in caixa:
            if r["grade"] == g:
                s.setdefault(str(r["ano"]), []).append(
                    {"semana": int(r["semana"]), "valor": num(r["preco"])})
        for a in s:
            s[a].sort(key=lambda x: x["semana"])
        return s

    hass = serie("Hass 18")
    if not hass:
        raise RuntimeError(
            "sem Hass 18 na view — o card de referência não virou linha; "
            "rode o etl_europa_cirad.py antes de regravar o embutido")
    h18 = [r for r in caixa if r["grade"] == "Hass 18"]

    E = D["precos"]["europa"]
    E["series"], E["green"] = hass, serie("Green 18")

    def grades(ano, sem):
        return sorted(
            [{"grade": r["grade"], "preco": num(r["preco"]), "unidade": r["unidade"],
              "variacao_eur": num(r.get("variacao_eur")),
              "variacao_media_pct": num(r.get("variacao_media_pct"))}
             for r in l if int(r["ano"]) == ano and int(r["semana"]) == sem],
            key=lambda x: x["grade"])

    E["ref_por_ano"], E["grades_por_ano"] = {}, {}
    for i, r in enumerate(h18):
        pen = h18[i - 1] if i else None
        E["ref_por_ano"][str(r["ano"])] = {
            "ano": int(r["ano"]), "semana": int(r["semana"]), "valor": num(r["preco"]),
            "variacao_eur": num(r.get("variacao_eur")),
            "variacao_media_pct": num(r.get("variacao_media_pct")),
            "relatorio_semana": num(r.get("relatorio_semana")),
            "anterior": ({"ano": int(pen["ano"]), "semana": int(pen["semana"]),
                          "valor": num(pen["preco"])} if pen else None),
            "em": r["extracted_at"]}
        E["grades_por_ano"][str(r["ano"])] = grades(int(r["ano"]), int(r["semana"]))
    ult = h18[-1]
    E["referencia"] = E["ref_por_ano"][str(ult["ano"])]
    E["grades"] = E["grades_por_ano"][str(ult["ano"])]
    E["embutido"] = True
    E["embutido_em"] = date.today().isoformat()
    E["carga_banco"] = ult["extracted_at"]
    return len(l)


def bloco_news(D) -> int:
    """espelho de carregaNews() + carregaCambio(): 7 países × 3 categorias"""
    l = sb("news", {"select": "data,pais,categoria,tag,impacto,estado,texto,fonte_url",
                    "order": "data.desc"})
    if not l:
        raise RuntimeError("news vazia")
    novo = {}
    for r in l:
        k = slug(r["pais"])
        d = novo.setdefault(k, {"pais": r["pais"], "geral": [], "regulacao": [],
                                "logistica": []})
        cat = r["categoria"] if r["categoria"] in CATEGORIAS else "geral"
        d[cat].append({"data": r["data"], "tag": r["tag"], "impacto": r["impacto"],
                       "texto": r["texto"], "fonte": r["fonte_url"]})
    for k, v in novo.items():
        D["news"][k] = {**D["news"].get(k, {}), **v}

    c = sb("v_cambio_atual", {"select": "par,data,valor", "order": "par.asc"})
    porpar = {r["par"]: r for r in c}
    for k, par in PAR_DE_PAIS.items():
        r = porpar.get(par)
        if not r:
            continue
        D["news"].setdefault(k, {"geral": [], "regulacao": [], "logistica": []})
        D["news"][k]["cambio"] = {"par": par, "valor": num(r["valor"]), "data": r["data"]}
    return len(l) + len(c)


def bloco_cirad(S) -> int:
    """
    espelho de carregaCirad() — este vai para SNAP.cirad, não para DATA.

    Antes, o fallback do resumo era o snapshot do PDF do Power BI (semana 24 de
    2026). O resumo é o único dado do projeto escrito por modelo de linguagem,
    então a procedência (modelo, método, conferência de cifras) faz parte do
    dado e vem junto.
    """
    campos = ("ano,semana,ordem,secao,texto,arquivo,relatorio_semana,modelo,"
              "metodo,conferido,extracted_at")
    desta = sb("v_cirad_resumo_atual", {"select": campos, "order": "ordem.asc"})
    if not desta:
        raise RuntimeError("v_cirad_resumo_atual vazia")
    p = desta[0]
    secoes = []
    for r in desta:
        linhas = [x.strip() for x in str(r["texto"]).split("\n") if x.strip()]
        itens = [re.sub(r"^-\s*", "", x) for x in linhas if x.startswith("-")]
        texto = " ".join(x for x in linhas if not x.startswith("-"))
        secoes.append({"titulo": r["secao"], "texto": texto or None, "itens": itens}
                      if itens else {"titulo": r["secao"], "texto": texto})
    S["cirad"] = {
        "titulo": S.get("cirad", {}).get(
            "titulo", "Resumo de Preços – Abacate Hass (CIRAD / Tropisens)"),
        "ano": int(p["ano"]), "semana": int(p["semana"]),
        "meta": {"temMeta": True, "arquivo": p.get("arquivo"),
                 "relatorio_semana": num(p.get("relatorio_semana")),
                 "modelo": p.get("modelo"), "metodo": p.get("metodo"),
                 "conferido": p.get("conferido") is True, "em": p["extracted_at"]},
        "secoes": secoes}
    return len(desta)


# ── ARQUIVO ───────────────────────────────────────────────────────────────
RE_BLOCO = r"^const {nome} = (\{{.*\}});$"


def le_bloco(html: str, nome: str) -> tuple[dict, str]:
    """(objeto, linha original) do `const NOME = {...};`"""
    m = re.search(RE_BLOCO.format(nome=nome), html, re.M)
    if not m:
        raise RuntimeError(f"não achei o bloco `const {nome} = ...;` no arquivo")
    if len(re.findall(RE_BLOCO.format(nome=nome), html, re.M)) > 1:
        raise RuntimeError(f"achei mais de um `const {nome}` — não sei qual trocar")
    return json.loads(m.group(1)), m.group(0)


def confere(antes: dict, depois: dict) -> list[str]:
    """
    Série do ano corrente não pode ENCOLHER. Um erro de leitura que devolve
    poucas linhas passaria calado — o embutido ficaria menor e ninguém veria,
    porque só aparece quando o Supabase cai.
    """
    problemas, ano = [], str(date.today().year)
    for k in ("brasil", "chile", "argentina", "uruguai", "europa"):
        a = len((antes.get("precos", {}).get(k, {}).get("series") or {}).get(ano) or [])
        d = len((depois.get("precos", {}).get(k, {}).get("series") or {}).get(ano) or [])
        if d < a:
            problemas.append(f"precos.{k}/{ano}: {a} semanas -> {d}")
    for k, v in (antes.get("news") or {}).items():
        for cat in CATEGORIAS:
            a = len(v.get(cat) or [])
            d = len(((depois.get("news") or {}).get(k) or {}).get(cat) or [])
            if d < a:
                problemas.append(f"news.{k}.{cat}: {a} -> {d}")
    return problemas


def main() -> int:
    argv = sys.argv
    dry = "--dry-run" in argv
    if "--arquivo" in argv:
        alvos = [argv[argv.index("--arquivo") + 1]]
    else:
        alvos = [a for a in ALVOS_HTML if Path(a).exists()]
    if not alvos:
        print(f"  ERRO: nenhum de {ALVOS_HTML} nesta pasta.")
        return 1

    agora = datetime.now(timezone.utc)
    print(f"Embutido do BI Avocado · {agora:%Y-%m-%d %H:%M} UTC")

    base = Path(alvos[0]).read_text(encoding="utf-8")
    DATA, linha_data = le_bloco(base, "DATA")
    SNAP, linha_snap = le_bloco(base, "SNAP")
    antes = json.loads(json.dumps(DATA))

    for nome, fn, arg in (("brasil", bloco_brasil, DATA), ("origens", bloco_origens, DATA),
                          ("europa", bloco_europa, DATA), ("news", bloco_news, DATA),
                          ("cirad", bloco_cirad, SNAP)):
        # sem try/except de propósito: embutido pela metade é pior que embutido
        # velho, porque metade dele passaria a contradizer a outra
        print(f"  {nome}: {fn(arg)} linhas")

    problemas = confere(antes, DATA)
    if problemas:
        print("  ERRO: o dado do banco veio MENOR que o embutido atual:")
        for p in problemas:
            print(f"    {p}")
        print("  Nada foi gravado. Rodar de novo costuma resolver; se repetir, "
              "o ETL da fonte é que não está gravando.")
        return 1

    DATA["meta"]["gerado_em"] = agora.isoformat()
    DATA["meta"]["embutido"] = {
        "gerado_por": "gerar_embutido.py",
        "semana_cirad": SNAP["cirad"]["semana"], "ano_cirad": SNAP["cirad"]["ano"],
        "ref_europa": DATA["precos"]["europa"]["referencia"]["valor"],
        "semana_europa": DATA["precos"]["europa"]["referencia"]["semana"]}

    nova_data = "const DATA = " + json.dumps(DATA, ensure_ascii=False, separators=SEP) + ";"
    nova_snap = "const SNAP = " + json.dumps(SNAP, ensure_ascii=False, separators=SEP) + ";"

    E = DATA["precos"]["europa"]["referencia"]
    print(f"\n  referência Europa: S{E['semana']}/{E['ano']} = {E['valor']} EUR")
    print(f"  resumo CIRAD:      S{SNAP['cirad']['semana']}/{SNAP['cirad']['ano']}"
          f" ({len(SNAP['cirad']['secoes'])} seções)")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 0

    for alvo in alvos:
        p = Path(alvo)
        html = p.read_text(encoding="utf-8")
        d_ant = len(html)
        html = html.replace(linha_data, nova_data).replace(linha_snap, nova_snap)
        # as travas do projeto: um </html>, um const de cada, JSON que volta a
        # parsear, e arquivo que não encolheu de repente
        if html.count("</html>") != 1:
            print(f"  ERRO {alvo}: {html.count('</html>')} ocorrências de </html>")
            return 1
        for nome in ("DATA", "SNAP"):
            le_bloco(html, nome)
        if len(html) < d_ant * 0.9:
            print(f"  ERRO {alvo}: arquivo caiu de {d_ant} para {len(html)} bytes")
            return 1
        if html == p.read_text(encoding="utf-8"):
            print(f"  {alvo}: sem mudança")
            continue
        # regra do projeto: HTML grande se escreve inteiro, nunca por edição parcial
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  {alvo}: gravado ({len(html)} bytes)")

    with open(ALVO_JSON, "w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, separators=SEP)
    print(f"  {ALVO_JSON}: gravado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
