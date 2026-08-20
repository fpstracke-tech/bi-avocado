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
civil com TODAS as frutas e hortaliças. Palta Hass em 2026: 5.938 registros,
156 dias, 11 mercados, com Precio minimo/maximo/promedio de verdade.

    https://datos.odepa.gob.cl/dataset/precios-mayoristas-de-frutas-y-hortalizas

Normalização de unidade — a ODEPA mistura duas famílias no mesmo produto:

    "$/kilo (en caja de 17 kilos)"   -> já é CLP/kg, usa direto
    "$/bandeja 10 kilos"             -> CLP pela bandeja, divide por 10

Sem isso a bandeja entra 10x maior e destrói a média. Validado: depois de
normalizar, as 8 unidades convergem para medianas de 2.100 a 4.900 CLP/kg;
antes, iam de 2.100 a 42.000.

Câmbio: ver `carregar_cambio`. Quatro camadas, começando pelo próprio banco.
Gravado junto com o registro — a série não se revaloriza retroativamente.

Uso:
    python etl_chile_odepa.py
    python etl_chile_odepa.py --dry-run
    python etl_chile_odepa.py --ano 2025        # recarga histórica
    python etl_chile_odepa.py --sem-cache       # ignora o banco, só fonte externa
"""

import csv
import io
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

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

URL_CAMBIO       = "https://mindicador.cl/api/dolar/{ano}"
URL_CAMBIO_STOOQ = "https://stooq.com/q/d/l/?s=usdclp&i=d"
URL_CAMBIO_YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/CLP=X"
                    "?period1={p1}&period2={p2}&interval=1d")

FX_MIN, FX_MAX  = 500.0, 2000.0   # faixa de sanidade CLP/USD
FX_TOLERANCIA   = 4               # dias que uma cotação anterior pode cobrir

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


# ── CÂMBIO ─────────────────────────────────────────────────────────────────────

def _curto(e, n: int = 140) -> str:
    """Erro de SSL vira parágrafo no log. Uma linha basta para diagnosticar."""
    t = " ".join(str(e).split())
    return t if len(t) <= n else t[:n] + "…"


def _sanos(fx: dict) -> dict:
    """Descarta cotação fora de 500–2000 CLP/USD. Fonte errada não entra calada."""
    return {d: v for d, v in fx.items() if v and FX_MIN < float(v) < FX_MAX}


def _fx_do_banco(ano: int) -> dict:
    """
    Camada 1 — o que JÁ ESTÁ no Supabase.

    `precos_origem.cotacao_local` guarda a taxa usada em cada dia desde a
    primeira carga. Então recarregar o ano inteiro não exige pedir de novo a
    nenhum site aquilo que o banco já tem, e o histórico fica imune a queda de
    API. Também é o que garante a promessa do cabeçalho: a série não se
    revaloriza retroativamente, porque o valor gravado sempre vence o novo.
    """
    try:
        import supabase_upsert
    except ImportError:
        return {}
    linhas = supabase_upsert.select(TABELA, {
        "select": "data,cotacao_local",
        "pais":   f"eq.{PAIS}",
        "data":   f"gte.{ano}-01-01",
        "and":    f"(data.lte.{ano}-12-31)",
        "limit":  "20000",
    })
    fx = _sanos({l["data"][:10]: float(l["cotacao_local"])
                 for l in linhas if l.get("cotacao_local")})
    if fx:
        print(f"  câmbio/banco: {len(fx)} dias já gravados", flush=True)
    return fx


def _fx_mindicador(ano: int, tentativas: int = 3):
    """Dólar observado BCCh. Fonte externa preferencial: é a taxa oficial."""
    for i in range(tentativas):
        try:
            r = requests.get(URL_CAMBIO.format(ano=ano), headers=HEADERS, timeout=90)
            r.raise_for_status()
            fx = _sanos({p["fecha"][:10]: float(p["valor"])
                         for p in r.json().get("serie", [])})
            if not fx:
                raise RuntimeError("série de câmbio vazia")
            return fx, "mindicador.cl (dólar observado BCCh)"
        except Exception as e:                       # noqa: BLE001
            print(f"  câmbio/mindicador: tentativa {i+1}/{tentativas} falhou "
                  f"({_curto(e)})", flush=True)
    return {}, None


def _fx_stooq(ano: int):
    """CSV diário Date,Open,High,Low,Close. Sem chave, sem limite conhecido."""
    try:
        r = requests.get(URL_CAMBIO_STOOQ, headers=HEADERS, timeout=60)
        r.raise_for_status()
        txt = r.text
        if not txt.lower().lstrip().startswith("date"):
            raise RuntimeError(f"resposta não é CSV: {txt[:60]!r}")
        fx = {}
        for row in csv.DictReader(io.StringIO(txt)):
            d = (row.get("Date") or "")[:10]
            if not d.startswith(str(ano)):
                continue
            try:
                fx[d] = float(row.get("Close") or 0)
            except ValueError:
                continue
        fx = _sanos(fx)
        if not fx:
            raise RuntimeError(f"nenhuma cotação de {ano} no CSV")
        return fx, "stooq.com USDCLP (fechamento de mercado)"
    except Exception as e:                           # noqa: BLE001
        print(f"  câmbio/stooq: falhou ({_curto(e)})", flush=True)
        return {}, None


def _fx_yahoo(ano: int):
    """CLP=X. Yahoo bloqueia alguns IPs — por isso é a última, não a primeira."""
    try:
        p1 = int(datetime(ano - 1, 12, 1, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(ano + 1, 1, 2, tzinfo=timezone.utc).timestamp())
        r = requests.get(URL_CAMBIO_YAHOO.format(p1=p1, p2=p2),
                         headers=HEADERS, timeout=60)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        fx = {}
        for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c is None:
                continue
            d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
            if d.startswith(str(ano)):
                fx[d] = float(c)
        fx = _sanos(fx)
        if not fx:
            raise RuntimeError(f"nenhuma cotação de {ano} na série")
        return fx, "Yahoo CLP=X (fechamento de mercado)"
    except Exception as e:                           # noqa: BLE001
        print(f"  câmbio/yahoo: falhou ({_curto(e)})", flush=True)
        return {}, None


def fx_da_data(fx: dict, d: str, tolerancia: int = FX_TOLERANCIA):
    """
    Dólar do dia; se não houver (feriado, fim de semana), o último anterior —
    mas só até `tolerancia` dias atrás. Nunca um valor fixo, e nunca uma taxa
    velha demais: arrastar a cotação de duas semanas atrás para um dia novo é
    inventar preço em dólar tão silenciosamente quanto o fallback de 950 do
    coletor antigo, só que mais difícil de perceber.
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


def carregar_cambio(ano: int, datas: list[str], sem_cache: bool = False):
    """
    {'AAAA-MM-DD': CLP por USD} + descrição das fontes usadas.

    Quatro camadas, nessa ordem:

        1. Supabase (precos_origem.cotacao_local) — o que já foi gravado
        2. mindicador.cl — dólar observado BCCh, a taxa oficial
        3. stooq.com USDCLP
        4. Yahoo CLP=X

    A camada 1 vem primeiro e VENCE nas datas que cobre: o histórico não se
    revaloriza e a recarga do ano não depende de nenhum site. As externas são
    consultadas apenas se sobrar dia descoberto — numa rodada diária normal é
    um dia só, e se o banco já cobre tudo nenhuma chamada externa acontece.

    Histórico do problema: mindicador.cl deu Read timeout em 11/08/2026 e
    SSLEOFError (corte de TLS, provável WAF contra IP de datacenter) em
    20/08/2026. Nesse segundo caso o CSV da ODEPA baixou inteiro e os 156 dias
    de Santiago foram descartados por falta de câmbio — o dado estava na mão e
    foi jogado fora porque uma API de terceiro caiu. Com a camada 1 isso não se
    repete: 155 dos 156 dias já estavam no banco.

    Sem NENHUMA das quatro, o ETL segue abortando. Taxa fixa no código não é
    plano B.
    """
    fx = {} if sem_cache else _fx_do_banco(ano)
    if sem_cache:
        print("  câmbio: --sem-cache, ignorando o banco", flush=True)
    origens = ["Supabase (histórico)"] if fx else []

    faltando = [d for d in datas if fx_da_data(fx, d) is None]
    if not faltando:
        print(f"  câmbio: banco cobre os {len(datas)} dias, "
              f"nenhuma consulta externa necessária", flush=True)
        return fx, " + ".join(origens)

    print(f"  câmbio: {len(faltando)} dia(s) sem cotação no banco "
          f"({faltando[0]} a {faltando[-1]}), buscando fonte externa", flush=True)

    for tentar in (_fx_mindicador, _fx_stooq, _fx_yahoo):
        novo, origem = tentar(ano)
        if not novo:
            continue
        # o banco vence: só preenche o que falta
        antes = len(faltando)
        for d, v in novo.items():
            fx.setdefault(d, v)
        faltando = [d for d in datas if fx_da_data(fx, d) is None]
        print(f"  câmbio: {origem} cobriu {antes - len(faltando)} dia(s)", flush=True)
        origens.append(origem)
        if not faltando:
            break

    if faltando:
        print(f"  AVISO: {len(faltando)} dia(s) seguem sem câmbio: {faltando[:5]}",
              flush=True)
    return fx, " + ".join(origens) if origens else "nenhuma"


# ── EXTRAÇÃO ───────────────────────────────────────────────────────────────────

def extrair(csv_txt: str, ano: int) -> dict:
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


def montar(por_data: dict, fx: dict) -> tuple[list[dict], list[str]]:
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
    return out, sem_fx


def main() -> int:
    dry       = "--dry-run" in sys.argv
    sem_cache = "--sem-cache" in sys.argv
    ano = date.today().year
    if "--ano" in sys.argv:
        ano = int(sys.argv[sys.argv.index("--ano") + 1])

    print(f"ETL Chile — Palta Hass atacado (ODEPA) · ano {ano} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    txt = baixar_csv(ano)
    print(f"  CSV baixado: {len(txt):,} caracteres")

    por_data = extrair(txt, ano)
    if not por_data:
        print("  ERRO: nenhum registro de Palta Hass em Santiago. "
              "O layout do CSV ou o nome dos mercados mudou.")
        return 1

    # o câmbio vem DEPOIS da extração, para pedir fora só os dias que faltam
    fx, fonte_fx = carregar_cambio(ano, sorted(por_data), sem_cache=sem_cache)

    regs, sem_fx = montar(por_data, fx)
    if not regs:
        print("  ERRO: nada a gravar — nenhum dia tem câmbio "
              "(banco vazio e as três fontes externas fora do ar).")
        return 1

    meds = [r["preco_medio_usd"] for r in regs]
    print(f"\n  {len(regs)} dias | {regs[0]['data']} a {regs[-1]['data']}")
    print(f"  câmbio: {fonte_fx}")
    print(f"  USD/kg médio: {min(meds):.2f} – {max(meds):.2f} "
          f"(média {sum(meds)/len(meds):.2f})")
    print("  Últimos 5 dias:")
    for r in regs[-5:]:
        print(f"    {r['data']}  {r['preco_min_usd']:.2f} / "
              f"{r['preco_medio_usd']:.2f} / {r['preco_max_usd']:.2f} USD/kg "
              f"@ {r['cotacao_local']:.2f} CLP")

    # Semáforo: buraco velho é aviso, dia mais recente sem câmbio é falha.
    # Um job vermelho todo dia treina a equipe a ignorar e-mail de falha, e aí
    # a falha que importa passa batido — mesma regra do etl_europa_cirad.
    ultimo = max(por_data)
    if sem_fx:
        print(f"\n  AVISO: {len(sem_fx)} dia(s) sem câmbio, fora da carga: "
              f"{sem_fx[:5]}{' …' if len(sem_fx) > 5 else ''}")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 1 if ultimo in sem_fx else 0

    print("\n[2] Upsert no Supabase...")
    import supabase_upsert
    res = supabase_upsert.upsert(TABELA, regs, on_conflict=CHAVE)
    if res["errors"]:
        for e in res["errors"][:3]:
            print(f"  ERRO lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1
    print(f"  OK: {res['inserted']} registros")

    if ultimo in sem_fx:
        print(f"  ERRO: o dia mais recente com preço ({ultimo}) ficou sem câmbio "
              f"e não entrou. O histórico foi gravado; o dado novo, não.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
