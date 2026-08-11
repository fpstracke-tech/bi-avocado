"""
import_fontes.py — carga inicial de todas as fontes do BI Avocado
=================================================================
BI Avocado — TFruits

Lê a pasta de origem do projeto e carrega no Supabase:

    news             notícias dos 7 países × 3 categorias
    cambio           7 pares USD/xxx
    precos_origem    Chile, Argentina, Uruguai (coletores diários)
    cirad_resumo     boletim CIRAD/Tropisens, por seção
    logistica_rotas  estudo de rotas TFruits (global + Brasil)
    janela_producao  janela mundial de produção Hass

`brasil_precos` NÃO é tratada aqui — tem script próprio
(`import_historico_brasil.py` + `etl_brasil_precos.py`).

Por que este script existe: os coletores atuais gravam CSV local e
**sobrescrevem** a cada rodada. As pastas `historico_*` têm um arquivo só, do
último dia. Ou seja, não existe histórico — cada execução apaga o dia anterior.
Depois desta carga, com UNIQUE nas tabelas, cada rodada passa a acumular.

Uso:
    python import_fontes.py --raiz "C:/Users/fpstr/ownCloud/TFruits PowerBI/Projeto Report Avocado"
    python import_fontes.py --raiz ... --dry-run
    python import_fontes.py --raiz ... --so news,cambio
"""

import csv
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import supabase_upsert

PAISES = {
    "brasil":   "Brasil",
    "chile":    "Chile",
    "colombia": "Colômbia",
    "espanha":  "Espanha",
    "israel":   "Israel",
    "marrocos": "Marrocos",
    "peru":     "Peru",
}

# categoria -> padrão do nome do arquivo dentro de newsletter_<pais>/
CATEGORIAS = {
    "geral":     "news_{p}.csv",
    "regulacao": "regulacao_{p}.csv",
    "logistica": "logistica_{p}.csv",
}

PRECOS = [
    ("Chile",     "Santiago",     "Chile/palta_santiago_precos.csv",              "clp_rate",  "USD/CLP"),
    ("Argentina", "Buenos Aires", "Argentina_precos/palta_buenosaires_precos.csv","blue_rate", "USD/ARS blue"),
    ("Uruguai",   "Montevideo",   "Uruguay_precos/palta_montevideo_precos.csv",   "uyu_rate",  "USD/UYU"),
]

MESES = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
         "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]


def limpa(s):
    return (s or "").strip()


def num(v):
    v = limpa(str(v)).replace(",", ".")
    if v in ("", "nan", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def le_csv(p: Path):
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def data_do_arquivo(p: Path) -> str:
    """Fallback: alguns CSVs antigos não têm coluna de data."""
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")


# ── NEWS ──────────────────────────────────────────────────────────────────────
def carrega_news(raiz: Path):
    linhas, avisos = [], []
    for slug, pais in PAISES.items():
        pasta = raiz / f"newsletter_{slug}"
        if not pasta.is_dir():
            avisos.append(f"pasta ausente: newsletter_{slug}")
            continue
        for cat, padrao in CATEGORIAS.items():
            arq = pasta / padrao.format(p=slug)
            if not arq.exists():
                continue
            fallback = data_do_arquivo(arq)
            n = 0
            for r in le_csv(arq):
                texto = limpa(r.get("texto"))
                if not texto:
                    continue
                linhas.append({
                    "data":      limpa(r.get("data")) or fallback,
                    "pais":      pais,
                    "categoria": cat,
                    "tag":       limpa(r.get("tag")) or None,
                    "impacto":   limpa(r.get("impacto")) or None,
                    "estado":    limpa(r.get("estado")) or None,
                    "texto":     texto,
                    "fonte_url": limpa(r.get("fonte_url")) or limpa(r.get("fonte")) or None,
                })
                n += 1
            if not limpa((le_csv(arq) or [{}])[0].get("data", "")):
                avisos.append(f"{arq.name}: sem coluna data, usei a data do arquivo ({fallback})")
            print(f"    {pais:10s} {cat:10s} {n:3d} registros  ({arq.name})")
    return "news", linhas, "pais,categoria,data,texto_hash", avisos


# ── CAMBIO ────────────────────────────────────────────────────────────────────
def carrega_cambio(raiz: Path):
    linhas, avisos = [], []
    for slug, pais in PAISES.items():
        arq = raiz / f"newsletter_{slug}" / f"news_{slug}_cambio.csv"
        if not arq.exists():
            avisos.append(f"sem câmbio para {pais}")
            continue
        for r in le_csv(arq):
            data = limpa(r.get("data")) or data_do_arquivo(arq)
            for col, val in r.items():
                if not col or not col.lower().startswith("usd_"):
                    continue
                v = num(val)
                if v is None:
                    continue
                par = "USD/" + col.split("_", 1)[1].upper()
                linhas.append({"data": data, "par": par, "valor": round(v, 6),
                               "fonte": "yfinance"})
                print(f"    {pais:10s} {par:12s} {v:>12.4f}  ({data})")
    return "cambio", linhas, "data,par", avisos


# ── PRECOS ORIGEM ─────────────────────────────────────────────────────────────
def carrega_precos_origem(raiz: Path):
    linhas, avisos = [], []
    for pais, cidade, rel, col_rate, par in PRECOS:
        arq = raiz / rel
        if not arq.exists():
            avisos.append(f"ausente: {rel}")
            continue
        n = 0
        for r in le_csv(arq):
            data = limpa(r.get("data"))
            if not data:
                continue
            linhas.append({
                "data": data,
                "pais": limpa(r.get("pais")) or pais,
                "cidade": limpa(r.get("cidade")) or cidade,
                "produto": limpa(r.get("produto")) or "Palta Hass",
                "unidade": limpa(r.get("unidade")) or "USD/kg",
                "preco_min_usd":   num(r.get("preco_min_usd")),
                "preco_max_usd":   num(r.get("preco_max_usd")),
                "preco_medio_usd": num(r.get("preco_medio_usd")),
                "cotacao_par":   par,
                "cotacao_local": num(r.get(col_rate)),
                "fonte": limpa(r.get("fonte")) or None,
            })
            n += 1
        datas = sorted(x["data"] for x in linhas if x["pais"] == pais or x["cidade"] == cidade)
        print(f"    {pais:10s} {n:3d} registros  {datas[0] if datas else '?'} a {datas[-1] if datas else '?'}")
    return "precos_origem", linhas, "data,pais,cidade,produto", avisos


# ── CIRAD ─────────────────────────────────────────────────────────────────────
def carrega_cirad(raiz: Path):
    arq = raiz / "Tropisens" / "Tropisens_summary.txt"
    if not arq.exists():
        return "cirad_resumo", [], "ano,semana,secao", [f"ausente: {arq}"]

    bruto = arq.read_text(encoding="utf-8-sig")
    linhas_txt = [l.rstrip() for l in bruto.splitlines()]

    ano = semana = None
    for l in linhas_txt[:6]:
        m = re.search(r"Semana\s+(\d{1,2}).{0,4}(\d{4})", l)
        if m:
            semana, ano = int(m.group(1)), int(m.group(2))
            break
    if not ano:
        return "cirad_resumo", [], "ano,semana,secao", ["não achei 'Semana N – AAAA' no txt"]

    def eh_cabecalho(l):
        """Seções começam com emoji/símbolo; itens de lista começam com '-'."""
        s = l.strip()
        if not s or s.startswith("-"):
            return False
        c = s[0]
        return not (c.isalnum() or c in "(\"'")

    secoes, atual, corpo = [], None, []
    for l in linhas_txt[1:]:
        if eh_cabecalho(l):
            if atual and corpo:
                secoes.append((atual, "\n".join(corpo).strip()))
            # remove só o prefixo (emoji/bandeira) — preserva símbolos internos como €
            atual = re.sub(r"^[^\w(]+", "", l.strip(), flags=re.UNICODE).strip()
            corpo = []
        elif atual:
            corpo.append(l.strip())
    if atual and corpo:
        secoes.append((atual, "\n".join(corpo).strip()))

    linhas = [{"ano": ano, "semana": semana, "ordem": i + 1, "secao": sec, "texto": txt}
              for i, (sec, txt) in enumerate(secoes) if txt]
    print(f"    semana {semana}/{ano} — {len(linhas)} seções")
    for r in linhas:
        print(f"      {r['ordem']}. {r['secao'][:58]}  ({len(r['texto'])} chars)")
    return "cirad_resumo", linhas, "ano,semana,secao", []


# ── LOGISTICA ─────────────────────────────────────────────────────────────────
def carrega_rotas(raiz: Path):
    import pandas as pd
    linhas, avisos = [], []

    g = raiz / "T-FRUITS Estudo de rotas.xlsx"
    if g.exists():
        d = pd.read_excel(g)
        d.columns = [str(c).strip() for c in d.columns]
        for r in d.itertuples(index=False):
            v = dict(zip(d.columns, r))
            if not limpa(str(v.get("País"))) or str(v.get("País")) == "nan":
                continue
            linhas.append({
                "pais_origem":   limpa(str(v.get("País"))),
                "porto_origem":  limpa(str(v.get("Porto Origem"))),
                "rota":          limpa(str(v.get("Rota"))).replace("nan", "") or "",
                "pais_destino":  limpa(str(v.get("País Destino"))),
                "porto_destino": limpa(str(v.get("Porto Destino"))),
                "modal":         "Marítimo",
                "transit_time_dias": num(v.get("Transit time estimado")),
                "armador":       limpa(str(v.get("Fornecedor"))).replace("nan", "") or "",
                "obs":           None,
                "fonte":         g.name,
            })
        print(f"    global: {len(linhas)} rotas ({g.name})")
    else:
        avisos.append(f"ausente: {g.name}")

    b = raiz / "T-FRUITS Estudo de rotas - Brasil_v2.xlsx"
    if b.exists():
        d = pd.read_excel(b)
        d.columns = [str(c).strip() for c in d.columns]
        n0 = len(linhas)
        for r in d.itertuples(index=False):
            v = dict(zip(d.columns, r))
            if not limpa(str(v.get("País"))) or str(v.get("País")) == "nan":
                continue
            linhas.append({
                "pais_origem":   limpa(str(v.get("País"))),
                "porto_origem":  limpa(str(v.get("Porto de origem"))),
                "rota":          "",
                "pais_destino":  limpa(str(v.get("País de destino"))),
                "porto_destino": limpa(str(v.get("Porto de Destino"))),
                "modal":         limpa(str(v.get("Modal"))) or "Marítimo",
                "transit_time_dias": num(v.get("Transit time estimado")),
                "armador":       limpa(str(v.get("Fornecedor"))).replace("nan", "") or "",
                "obs":           (limpa(str(v.get("Obs"))) or None) if str(v.get("Obs")) != "nan" else None,
                "fonte":         b.name,
            })
        print(f"    brasil: {len(linhas)-n0} rotas ({b.name})")
    else:
        avisos.append(f"ausente: {b.name}")

    # o UNIQUE é por (origem, porto, destino, porto, modal) — remove colisões
    vistos, dedup = set(), []
    for x in linhas:
        k = (x["pais_origem"], x["porto_origem"], x["pais_destino"], x["porto_destino"],
             x["modal"], x["rota"], x["armador"])
        if k in vistos:
            avisos.append(f"rota duplicada ignorada: {' → '.join(str(i) for i in k)}")
            continue
        vistos.add(k)
        dedup.append(x)
    return ("logistica_rotas", dedup,
            "pais_origem,porto_origem,pais_destino,porto_destino,modal,rota,armador", avisos)


# ── JANELA DE PRODUÇÃO ────────────────────────────────────────────────────────
def carrega_janela(raiz: Path):
    arq = raiz / "janela_avocado_ordenada_producao_ordempais.csv"
    if not arq.exists():
        return "janela_producao", [], "pais,mes_num", [f"ausente: {arq.name}"]
    linhas = []
    for r in le_csv(arq):
        mes = limpa(r.get("Mês"))
        if mes not in MESES:
            continue
        linhas.append({
            "pais":       limpa(r.get("País")),
            "mes_num":    MESES.index(mes) + 1,
            "mes_nome":   mes,
            "nivel":      int(num(r.get("NívelProdução")) or 0),
            "ordem_pais": int(num(r.get("OrdemPais")) or 0) or None,
        })
    paises = sorted({x["pais"] for x in linhas})
    print(f"    {len(linhas)} registros, {len(paises)} países: {', '.join(paises)}")
    return "janela_producao", linhas, "pais,mes_num", []


# ── MAIN ──────────────────────────────────────────────────────────────────────
BLOCOS = {
    "news":            carrega_news,
    "cambio":          carrega_cambio,
    "precos_origem":   carrega_precos_origem,
    "cirad_resumo":    carrega_cirad,
    "logistica_rotas": carrega_rotas,
    "janela_producao": carrega_janela,
}


def main() -> int:
    if "--raiz" not in sys.argv:
        print("ERRO: informe --raiz com a pasta 'Projeto Report Avocado'.")
        return 1
    raiz = Path(sys.argv[sys.argv.index("--raiz") + 1])
    if not raiz.is_dir():
        print(f"ERRO: {raiz} não é uma pasta.")
        return 1
    dry = "--dry-run" in sys.argv
    quais = list(BLOCOS)
    if "--so" in sys.argv:
        quais = [x.strip() for x in sys.argv[sys.argv.index("--so") + 1].split(",")]

    print(f"Carga de fontes — BI Avocado — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Raiz: {raiz}")

    total, erros, todos_avisos = {}, 0, []
    for nome in quais:
        fn = BLOCOS.get(nome)
        if not fn:
            print(f"\n[{nome}] bloco desconhecido, pulando")
            continue
        print(f"\n[{nome}]")
        tabela, linhas, on_conflict, avisos = fn(raiz)
        todos_avisos += [f"{nome}: {a}" for a in avisos]
        total[tabela] = len(linhas)
        if not linhas:
            print("    nada a carregar")
            continue
        if dry:
            print(f"    --dry-run: {len(linhas)} registros prontos")
            continue
        res = supabase_upsert.upsert(tabela, linhas, on_conflict=on_conflict)
        print(f"    Supabase {tabela}: {res['inserted']} de {len(linhas)} enviados")
        for e in res["errors"]:
            print(f"    ERRO lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
            erros += 1

    print("\n" + "=" * 62)
    for t, n in total.items():
        print(f"  {t:18s} {n:5d} registros")
    if todos_avisos:
        print("\nAvisos:")
        for a in todos_avisos:
            print(f"  - {a}")
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
