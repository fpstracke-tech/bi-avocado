"""
ETL Preços Chile — Palta Hass, atacado (ODEPA)
===============================================
SUBSTITUI o palta_santiago_collector.py, que NÃO coletava nada: ele gravava
`preco_clp_kg = 3450.0` fixo no código e só variava o câmbio. A URL da ODEPA
estava declarada e nunca era requisitada. Auditoria de 11/08/2026: nas 28
linhas dessa fonte, preco_medio_usd x cotacao_local dava 3.448–3.455 sempre, e
a razão max/min era 1,1739 em todas (= 1,08 / 0,92). A série no dashboard era
o gráfico do dólar/peso invertido.

Fonte real: mesmo CSV oficial que o BI Limão já consome, um arquivo por ano
civil com TODAS as frutas e hortaliças. Palta Hass em 2026: 5.787 registros,
151 dias, 11 mercados, com Precio minimo/maximo/promedio de verdade.

    https://datos.odepa.gob.cl/dataset/precios-mayoristas-de-frutas-y-hortalizas

Normalização de unidade — a ODEPA mistura duas famílias no mesmo produto:

    "$/kilo (en caja de 17 kilos)"   -> já é CLP/kg, usa direto
    "$/bandeja 10 kilos"             -> CLP pela bandeja, divide por 10

Sem isso a bandeja entra 10x maior e destrói a média. Validado: depois de
normalizar, as 8 unidades convergem para medianas de 2.100 a 4.900 CLP/kg;
antes, iam de 2.100 a 42.000.

Câmbio: dólar observado do Banco Central do Chile (mindicador.cl), por data.
Gravado junto com o registro — a série não se revaloriza retroativamente.

Uso:
    python etl_chile_odepa.py
    python etl_chile_odepa.py --dry-run
    python etl_chile_odepa.py --ano 2025      # recarga histórica
"""

import csv
import io
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import requests

TABELA   = "precos_origem"
CHAVE    = "data,pais,cidade,produto"
PAIS     = "Chile"
CIDADE   = "Santiago"
PRODUTO  = "Palta Hass"
FONTE    = "ODEPA — precios mayoristas (datos.odepa.gob.cl)"

# Mercados de Santiago. A ODEPA cobre 11 praças no país; a série do dashboard é
# de Santiago, então filtro aqui em vez de misturar Temuco e Arica na média.
MERCADOS_SANTIAGO = (
    "Mercado Mayorista Lo Valledor de Santiago",
    "Vega Central Mapocho de Santiago",
)

URL_CSV = (
    "https://datos.odepa.gob.cl/dataset/33f10516-acbe-4446-b633-68244b9b6b26"
    "/resource/580beca0-e87e-4dd4-9e8a-0bd92773f4a6"
    "/download/precio_mayorista_fruta-hortaliza_{ano}.csv"
)
URL_CAMBIO = "https://mindicador.cl/api/dolar/{ano}"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://datos.odepa.gob.cl/",
}

RE_EMBALAGEM = re.compile(r"\$/(?:bandeja|caja|bins)\s*(?:de\s*)?(\d+)\s*kilos", re.I)


def num(s):
    """'3600,0000' -> 3600.0 · '1.234,50' -> 1234.5"""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def clp_por_kg(unidade: str, valor: float):
    """Normaliza qualquer unidade de comercialização da ODEPA para CLP/kg."""
    if valor is None or not unidade:
        return None
    u = unidade.strip().lower()
    if u.startswith("$/kilo"):
        return valor                      # já é por quilo
    m = RE_EMBALAGEM.search(u)
    if m:
        kg = float(m.group(1))
        return valor / kg if kg > 0 else None
    return None                           # unidade desconhecida: descarta e avisa


def baixar_csv(ano: int, tentativas: int = 4) -> str:
    """
    O CSV da ODEPA é o ano civil inteiro com TODAS as frutas e hortaliças:
    ~25 MB, 112 mil linhas. Numa conexão comum isso leva de 30s a alguns
    minutos. Por isso o download é em stream e com progresso na tela — sem
    isso o script fica minutos calado e parece travado.
    """
    url = URL_CSV.format(ano=ano)
    erro = None
    for i in range(tentativas):
        try:
            print(f"  baixando {url.rsplit('/', 1)[-1]} (~25 MB, pode levar "
                  f"alguns minutos)...", flush=True)
            r = requests.get(url, headers=HEADERS, timeout=300, stream=True)
            r.raise_for_status()

            total = int(r.headers.get("Content-Length") or 0)
            pedacos, baixado, marco = [], 0, 0
            for pedaco in r.iter_content(chunk_size=1 << 20):   # 1 MB
                if not pedaco:
                    continue
                pedacos.append(pedaco)
                baixado += len(pedaco)
                if baixado - marco >= 5 << 20:                  # a cada 5 MB
                    marco = baixado
                    if total:
                        print(f"    {baixado/1e6:.0f} de {total/1e6:.0f} MB "
                              f"({100*baixado/total:.0f}%)", flush=True)
                    else:
                        print(f"    {baixado/1e6:.0f} MB", flush=True)

            dados = b"".join(pedacos)
            if len(dados) < 100_000:
                raise RuntimeError(f"CSV suspeito de truncado: {len(dados)} bytes")
            print(f"  download concluído: {len(dados)/1e6:.1f} MB", flush=True)
            return dados.decode("utf-8-sig", errors="replace")
        except Exception as e:                       # noqa: BLE001
            erro = e
            print(f"  tentativa {i+1}/{tentativas} falhou: {e}", flush=True)
    raise RuntimeError(f"não consegui baixar o CSV da ODEPA: {erro}")


def carregar_cambio(ano: int, tentativas: int = 4) -> dict:
    """
    {'AAAA-MM-DD': CLP por USD} — dólar observado BCCh.

    A mindicador.cl cai com alguma frequência (Read timeout observado em
    11/08/2026). Sem câmbio o ETL ABORTA em vez de usar taxa fixa: era
    exatamente o fallback silencioso de 950 do coletor antigo que fazia a série
    parecer viva quando não estava.
    """
    erro = None
    for i in range(tentativas):
        try:
            r = requests.get(URL_CAMBIO.format(ano=ano), timeout=90)
            r.raise_for_status()
            fx = {p["fecha"][:10]: float(p["valor"])
                  for p in r.json().get("serie", [])}
            if not fx:
                raise RuntimeError("série de câmbio vazia")
            print(f"  câmbio BCCh: {len(fx)} dias")
            return fx
        except Exception as e:                       # noqa: BLE001
            erro = e
            print(f"  câmbio: tentativa {i+1}/{tentativas} falhou ({e})")
    print(f"  ERRO: câmbio indisponível depois de {tentativas} tentativas ({erro})")
    return {}


def fx_da_data(fx: dict, d: str):
    """Dólar do dia; se não houver (feriado), o último anterior. Nunca um fixo."""
    if d in fx:
        return fx[d]
    anteriores = [k for k in fx if k <= d]
    return fx[max(anteriores)] if anteriores else None


def extrair(csv_txt: str, ano: int) -> list[dict]:
    por_data = defaultdict(lambda: {"min": [], "max": [], "med": [], "mercados": set()})
    desconhecidas, total_hass = set(), 0

    for r in csv.DictReader(io.StringIO(csv_txt)):
        if (r.get("Producto") or "").strip().lower() != "palta":
            continue
        if (r.get("Variedad / Tipo") or "").strip().lower() != "hass":
            continue
        total_hass += 1
        if (r.get("Mercado") or "").strip() not in MERCADOS_SANTIAGO:
            continue

        unidade = (r.get("Unidad de comercializacion")
                   or r.get("Unidad de comercialización") or "")
        mn = clp_por_kg(unidade, num(r.get("Precio minimo")))
        mx = clp_por_kg(unidade, num(r.get("Precio maximo")))
        md = clp_por_kg(unidade, num(r.get("Precio promedio")))
        if md is None:
            if unidade and not unidade.strip().lower().startswith("$/kilo") \
               and not RE_EMBALAGEM.search(unidade.lower()):
                desconhecidas.add(unidade)
            continue

        d = (r.get("Fecha") or "").strip()[:10]
        if not d.startswith(str(ano)):
            continue
        b = por_data[d]
        b["med"].append(md)
        b["min"].append(mn if mn is not None else md)
        b["max"].append(mx if mx is not None else md)
        b["mercados"].add((r.get("Mercado") or "").strip())

    print(f"  Palta Hass no país: {total_hass} linhas")
    print(f"  Santiago: {sum(len(v['med']) for v in por_data.values())} linhas "
          f"em {len(por_data)} dias")
    if desconhecidas:
        print(f"  AVISO: unidades sem regra de conversão, descartadas: {desconhecidas}")

    return por_data


def montar(por_data: dict, fx: dict) -> list[dict]:
    agora = datetime.now(timezone.utc).isoformat()
    out, sem_fx = [], []
    for d in sorted(por_data):
        b = por_data[d]
        taxa = fx_da_data(fx, d)
        if not taxa:
            sem_fx.append(d)
            continue
        out.append({
            "data":            d,
            "pais":            PAIS,
            "cidade":          CIDADE,
            "produto":         PRODUTO,
            "unidade":         "USD/kg",
            "preco_min_usd":   round(min(b["min"]) / taxa, 4),
            "preco_max_usd":   round(max(b["max"]) / taxa, 4),
            "preco_medio_usd": round((sum(b["med"]) / len(b["med"])) / taxa, 4),
            "cotacao_par":     "USD/CLP",
            "cotacao_local":   round(taxa, 4),
            "fonte":           FONTE,
            "extracted_at":    agora,
        })
    if sem_fx:
        print(f"  AVISO: {len(sem_fx)} dias sem câmbio, fora da carga: {sem_fx[:5]}")
    return out


def main() -> int:
    dry = "--dry-run" in sys.argv
    ano = date.today().year
    if "--ano" in sys.argv:
        ano = int(sys.argv[sys.argv.index("--ano") + 1])

    print(f"ETL Chile — Palta Hass atacado (ODEPA) · ano {ano} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    txt = baixar_csv(ano)
    print(f"  CSV baixado: {len(txt):,} caracteres")

    fx = carregar_cambio(ano)
    por_data = extrair(txt, ano)
    if not por_data:
        print("  ERRO: nenhum registro de Palta Hass em Santiago. "
              "O layout do CSV ou o nome dos mercados mudou.")
        return 1

    regs = montar(por_data, fx)
    if not regs:
        print("  ERRO: nada a gravar depois do câmbio.")
        return 1

    meds = [r["preco_medio_usd"] for r in regs]
    print(f"\n  {len(regs)} dias | {regs[0]['data']} a {regs[-1]['data']}")
    print(f"  USD/kg médio: {min(meds):.2f} – {max(meds):.2f} "
          f"(média {sum(meds)/len(meds):.2f})")
    print("  Últimos 5 dias:")
    for r in regs[-5:]:
        print(f"    {r['data']}  {r['preco_min_usd']:.2f} / "
              f"{r['preco_medio_usd']:.2f} / {r['preco_max_usd']:.2f} USD/kg "
              f"@ {r['cotacao_local']:.2f} CLP")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 0

    print("\n[2] Upsert no Supabase...")
    import supabase_upsert
    res = supabase_upsert.upsert(TABELA, regs, on_conflict=CHAVE)
    if res["errors"]:
        for e in res["errors"][:3]:
            print(f"  ERRO lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1
    print(f"  OK: {res['inserted']} registros")
    return 0


if __name__ == "__main__":
    sys.exit(main())
