"""
cambio_fx.py — câmbio datado para os ETLs de origem
====================================================
Uma implementação só da regra de câmbio, usada por Chile, Argentina e Uruguai.
Antes cada ETL tinha a sua, e regra duplicada é regra que diverge.

O PRINCÍPIO: o banco vem primeiro.

`precos_origem.cotacao_local` guarda a taxa usada em cada dia desde a primeira
carga. Então recarregar um ano inteiro não exige pedir de novo a nenhum site
aquilo que já está gravado. As fontes externas só são consultadas para os dias
que sobram — numa rodada diária normal, um dia.

Isso conserta uma classe inteira de falha, não um incidente:

    20/08/2026 — Chile. mindicador.cl devolveu SSLEOFError (corte de TLS,
    provável WAF contra IP de datacenter) nas 4 tentativas. O CSV de 26 MB da
    ODEPA já tinha baixado e os 156 dias de Santiago já estavam extraídos.
    Tudo foi descartado por falta de câmbio — sendo que 155 dos 156 dias já
    estavam no Supabase.

    11/08/2026 — Chile. Read timeout no mesmo mindicador.cl.

    Documentado como risco conhecido em BI_Avocado_Fontes_Reais_Origem.md:
    "precos_origem do Uruguai depende do Yahoo, que bloqueia alguns IPs."

O QUE NÃO MUDA: sem nenhuma camada, o ETL aborta. Taxa fixa no código não é
plano B — era exatamente o defeito dos coletores antigos (950 no Chile contra
914 real, 1.435 na Argentina contra 1.535), que faziam a série parecer viva
quando não estava.

Uso:
    import cambio_fx
    fx, fonte = cambio_fx.carregar("Chile", "USD/CLP", 2026, sorted(por_data))
    taxa = cambio_fx.fx_da_data(fx, "2026-08-19")
"""

import csv
import io
from datetime import date, datetime, timezone

import requests

TABELA_CACHE = "precos_origem"
TOLERANCIA   = 4          # dias que uma cotação anterior pode cobrir

# Faixa de sanidade por par. Fonte que devolve número fora disso está falando
# de outra coisa (outra moeda, outro campo, página de erro) e não entra calada.
FAIXAS = {
    "USD/CLP":      (500.0, 2000.0),
    "USD/ARS blue": (200.0, 50000.0),   # inflação: 1.000 em 2024, 1.500+ em 2026
    "USD/UYU":      (20.0, 100.0),
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "*/*",
}


def curto(e, n: int = 140) -> str:
    """Erro de SSL vira parágrafo no log. Uma linha basta para diagnosticar."""
    t = " ".join(str(e).split())
    return t if len(t) <= n else t[:n] + "…"


def _sanos(fx: dict, par: str) -> dict:
    lo, hi = FAIXAS.get(par, (0.0, float("inf")))
    saida = {}
    for d, v in fx.items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if lo < v < hi:
            saida[str(d)[:10]] = v
    return saida


def fx_da_data(fx: dict, d: str, tolerancia: int = TOLERANCIA):
    """
    Cotação do dia; se não houver (feriado, fim de semana), a última anterior —
    mas só até `tolerancia` dias atrás.

    O limite existe porque arrastar a cotação de duas semanas atrás para um dia
    novo é inventar preço em dólar tão silenciosamente quanto a taxa fixa no
    código, e mais difícil de perceber depois.
    """
    if d in fx:
        return fx[d]
    anteriores = [k for k in fx if k <= d]
    if not anteriores:
        return None
    k = max(anteriores)
    try:
        atraso = (date.fromisoformat(d) - date.fromisoformat(k)).days
    except ValueError:
        return None
    return fx[k] if atraso <= tolerancia else None


# ── CAMADA 1: O BANCO ──────────────────────────────────────────────────────────

def do_banco(pais: str, par: str, ano: int) -> dict:
    """
    O que já está gravado em precos_origem.cotacao_local para aquele país e ano.

    Vence as fontes externas nas datas que cobre: o histórico não se revaloriza
    retroativamente, e o valor que o dashboard mostra hoje é o mesmo de ontem.
    """
    try:
        import supabase_upsert
    except ImportError:
        return {}
    linhas = supabase_upsert.select(TABELA_CACHE, {
        "select": "data,cotacao_local",
        "pais":   f"eq.{pais}",
        "and":    f"(data.gte.{ano}-01-01,data.lte.{ano}-12-31)",
        "order":  "data.asc",
        "limit":  "20000",
    })
    fx = _sanos({l["data"]: l["cotacao_local"]
                 for l in linhas if l.get("cotacao_local")}, par)
    if fx:
        print(f"  câmbio/banco: {len(fx)} dias já gravados", flush=True)
    return fx


# ── CAMADAS EXTERNAS ───────────────────────────────────────────────────────────
# Cada fonte é uma função (ano) -> (dict, rótulo). Nunca levanta exceção: quem
# não conseguir devolve ({}, None) e a cascata segue para a próxima.

def mindicador_clp(ano: int, tentativas: int = 3):
    """Dólar observado do Banco Central do Chile. É a taxa oficial do país."""
    for i in range(tentativas):
        try:
            r = requests.get(f"https://mindicador.cl/api/dolar/{ano}",
                             headers=HEADERS, timeout=90)
            r.raise_for_status()
            fx = _sanos({p["fecha"][:10]: p["valor"]
                         for p in r.json().get("serie", [])}, "USD/CLP")
            if not fx:
                raise RuntimeError("série vazia")
            return fx, "mindicador.cl (dólar observado BCCh)"
        except Exception as e:                        # noqa: BLE001
            print(f"  câmbio/mindicador: tentativa {i+1}/{tentativas} falhou "
                  f"({curto(e)})", flush=True)
    return {}, None


def bluelytics_ars(ano: int, tentativas: int = 3):
    """Blue, value_sell. Em Argentina o blue é a taxa economicamente relevante."""
    for i in range(tentativas):
        try:
            r = requests.get("https://api.bluelytics.com.ar/v2/evolution.json",
                             headers=HEADERS, timeout=90)
            r.raise_for_status()
            fx = _sanos({p["date"]: p["value_sell"] for p in r.json()
                         if (p.get("source") or "").lower() == "blue"
                         and p.get("value_sell")}, "USD/ARS blue")
            if not fx:
                raise RuntimeError("série blue vazia")
            return fx, "bluelytics.com.ar (blue, value_sell)"
        except Exception as e:                        # noqa: BLE001
            print(f"  câmbio/bluelytics: tentativa {i+1}/{tentativas} falhou "
                  f"({curto(e)})", flush=True)
    return {}, None


def argentinadatos_ars(ano: int):
    """
    Série histórica de blue, provedor independente do bluelytics.

    Só entram fontes de BLUE nesta cascata. Oficial e blue não são a mesma
    taxa, e costurar as duas na mesma série produziria um degrau que ninguém
    conseguiria explicar depois olhando só o gráfico.
    """
    try:
        r = requests.get("https://api.argentinadatos.com/v1/cotizaciones/dolares/blue",
                         headers=HEADERS, timeout=60)
        r.raise_for_status()
        fx = _sanos({p["fecha"]: p.get("venta") for p in r.json()
                     if p.get("fecha", "").startswith(str(ano))}, "USD/ARS blue")
        if not fx:
            raise RuntimeError(f"nenhuma cotação de {ano}")
        return fx, "argentinadatos.com (blue, venta)"
    except Exception as e:                            # noqa: BLE001
        print(f"  câmbio/argentinadatos: falhou ({curto(e)})", flush=True)
        return {}, None


def dolarapi_ars_hoje(ano: int):
    """Blue de hoje. Cobre um dia só — que é o caso da rodada diária."""
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue",
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        j = r.json()
        d = (j.get("fechaActualizacion") or "")[:10] or \
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fx = _sanos({d: j.get("venta")}, "USD/ARS blue")
        if not fx:
            raise RuntimeError("sem venda no payload")
        return fx, "dolarapi.com (blue de hoje)"
    except Exception as e:                            # noqa: BLE001
        print(f"  câmbio/dolarapi: falhou ({curto(e)})", flush=True)
        return {}, None


def stooq(simbolo: str, par: str):
    """CSV diário Date,Open,High,Low,Close. Sem chave, sem limite conhecido."""
    def fonte(ano: int):
        try:
            r = requests.get(f"https://stooq.com/q/d/l/?s={simbolo}&i=d",
                             headers=HEADERS, timeout=60)
            r.raise_for_status()
            txt = r.text
            if not txt.lower().lstrip().startswith("date"):
                raise RuntimeError(f"resposta não é CSV: {txt[:60]!r}")
            bruto = {}
            for row in csv.DictReader(io.StringIO(txt)):
                d = (row.get("Date") or "")[:10]
                if d.startswith(str(ano)):
                    bruto[d] = row.get("Close")
            fx = _sanos(bruto, par)
            if not fx:
                raise RuntimeError(f"nenhuma cotação de {ano}")
            return fx, f"stooq.com {simbolo.upper()} (fechamento de mercado)"
        except Exception as e:                        # noqa: BLE001
            print(f"  câmbio/stooq {simbolo}: falhou ({curto(e)})", flush=True)
            return {}, None
    return fonte


def yahoo(ticker: str, par: str):
    """Yahoo Finance. Bloqueia alguns IPs — por isso nunca é a primeira."""
    def fonte(ano: int):
        try:
            p1 = int(datetime(ano - 1, 12, 1, tzinfo=timezone.utc).timestamp())
            p2 = int(datetime(ano + 1, 1, 2, tzinfo=timezone.utc).timestamp())
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?period1={p1}&period2={p2}&interval=1d",
                headers=HEADERS, timeout=60)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            bruto = {}
            for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c is None:
                    continue
                d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
                if d.startswith(str(ano)):
                    bruto[d] = c
            fx = _sanos(bruto, par)
            if not fx:
                raise RuntimeError(f"nenhuma cotação de {ano}")
            return fx, f"Yahoo {ticker} (fechamento de mercado)"
        except Exception as e:                        # noqa: BLE001
            print(f"  câmbio/yahoo {ticker}: falhou ({curto(e)})", flush=True)
            return {}, None
    return fonte


def erapi_hoje(moeda: str, par: str):
    """open.er-api.com — taxa de hoje, sem chave. Último recurso, cobre 1 dia."""
    def fonte(ano: int):
        try:
            r = requests.get("https://open.er-api.com/v6/latest/USD",
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            j = r.json()
            d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            fx = _sanos({d: (j.get("rates") or {}).get(moeda)}, par)
            if not fx:
                raise RuntimeError(f"sem {moeda} no payload")
            return fx, f"open.er-api.com USD/{moeda} (hoje)"
        except Exception as e:                        # noqa: BLE001
            print(f"  câmbio/er-api: falhou ({curto(e)})", flush=True)
            return {}, None
    return fonte


# Ordem: oficial//relevante primeiro, depois provedores de mercado.
CASCATAS = {
    "USD/CLP": [
        mindicador_clp,
        stooq("usdclp", "USD/CLP"),
        yahoo("CLP=X", "USD/CLP"),
    ],
    "USD/ARS blue": [
        bluelytics_ars,
        argentinadatos_ars,
        dolarapi_ars_hoje,
    ],
    "USD/UYU": [
        yahoo("UYU=X", "USD/UYU"),
        stooq("usduyu", "USD/UYU"),
        erapi_hoje("UYU", "USD/UYU"),
    ],
}


# ── ORQUESTRAÇÃO ───────────────────────────────────────────────────────────────

def carregar(pais: str, par: str, ano: int, datas: list[str],
             sem_cache: bool = False):
    """
    Devolve ({'AAAA-MM-DD': taxa}, descrição das fontes usadas).

    `datas` é a lista de dias que o ETL precisa cobrir — vem DEPOIS da extração,
    para que só se peça fora aquilo que falta. Se o banco já cobre tudo, nenhuma
    chamada externa acontece.

    Quem chama decide o que fazer com dia descoberto. A convenção do projeto:
    buraco antigo é aviso e o job termina verde; o dia mais recente ficar de
    fora é falha. Job vermelho todo dia treina a equipe a ignorar e-mail de
    falha, e aí a falha que importa passa batido.
    """
    fx = {}
    origens = []
    if sem_cache:
        print("  câmbio: --sem-cache, ignorando o banco", flush=True)
    else:
        fx = do_banco(pais, par, ano)
        if fx:
            origens.append("Supabase (histórico)")

    faltando = [d for d in datas if fx_da_data(fx, d) is None]
    if not faltando:
        print(f"  câmbio: banco cobre os {len(datas)} dias, nenhuma consulta "
              f"externa necessária", flush=True)
        return fx, " + ".join(origens)

    print(f"  câmbio: {len(faltando)} dia(s) sem cotação no banco "
          f"({faltando[0]} a {faltando[-1]}), buscando fonte externa", flush=True)

    for tentar in CASCATAS.get(par, []):
        novo, rotulo = tentar(ano)
        if not novo:
            continue
        antes = len(faltando)
        for d, v in novo.items():
            fx.setdefault(d, v)          # o banco vence: só preenche o que falta
        faltando = [d for d in datas if fx_da_data(fx, d) is None]
        print(f"  câmbio: {rotulo} cobriu {antes - len(faltando)} dia(s)",
              flush=True)
        origens.append(rotulo)
        if not faltando:
            break

    if faltando:
        print(f"  AVISO: {len(faltando)} dia(s) seguem sem câmbio: "
              f"{faltando[:5]}{' …' if len(faltando) > 5 else ''}", flush=True)

    return fx, (" + ".join(origens) if origens else "nenhuma")
