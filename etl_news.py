"""
etl_news.py — Notícias e câmbio dos 7 países, num script só
===========================================================
BI Avocado — TFruits

Fontes: SerpAPI (engine google_news) para as manchetes
        Yahoo Finance (yfinance) para as cotações
Alvo:   Supabase, tabelas `news` (upsert por pais+categoria+data+md5(texto))
        e `cambio` (upsert por data+par)

Substitui 28 scripts: 7 países × {geral, logistica, regulacao, cambio}
(`news_<pais>.py`, `news_<pais>_logistica.py`, `news_<pais>_regulacao.py`,
`news_<pais>_cambio.py`). País e categoria viraram parâmetro; as queries
vivem no dicionário QUERIES.

Quatro diferenças de comportamento em relação aos coletores antigos, todas
de propósito:

  1. **Acumula histórico.** Todo script antigo fazia `os.remove()` na pasta
     de histórico inteira antes de gravar, e o CSV principal era reescrito
     com `w`. Por isso nunca houve série de notícias: só existia a última
     rodada. Aqui é upsert e nada é apagado.

  2. **Dedupe por URL, não por texto.** A constraint da tabela é
     `UNIQUE (pais, categoria, data, md5(texto))`, e isso *não* basta:
     a tradução é feita por modelo de linguagem e não é determinística, então
     rodar duas vezes no mesmo dia produziria texto diferente, hash diferente
     e linha duplicada da mesma notícia. Antes de gravar, o script lê as URLs
     já presentes naquele (país, categoria, data) e descarta as conhecidas.
     A URL é o identificador estável da matéria; a manchete traduzida não é.

  3. **Tradução opcional e não fatal.** Se `ANTHROPIC_API_KEY` não existir,
     a manchete entra no idioma original com aviso, em vez de o job falhar.
     Mesmo princípio do `etl_cirad_resumo.py`: job vermelho todo dia treina
     a equipe a ignorar e-mail de falha.

  4. **Câmbio ausente não inventa nem aborta.** Os coletores antigos de preço
     tinham taxa fixa no código (950 no Chile contra 914 real). Aqui, par que
     não cotar é registrado como aviso e fica de fora — mas as notícias do dia
     entram. Câmbio é KPI de tela, não base de cálculo de preço.

Uso:
    python etl_news.py                          # 7 países × 3 categorias + câmbio
    python etl_news.py --pais chile,peru        # só esses países
    python etl_news.py --categoria regulacao    # só uma categoria
    python etl_news.py --sem-cambio             # pula as cotações
    python etl_news.py --so-cambio              # só as cotações
    python etl_news.py --sem-traducao           # mantém o título original
    python etl_news.py --dry-run                # mostra o que faria, não grava
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import requests

import supabase_upsert

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────────────────────

# slug -> (nome como vai para o banco, código de país do Google News)
PAISES = {
    "brasil":   ("Brasil",   "br"),
    "chile":    ("Chile",    "cl"),
    "colombia": ("Colômbia", "co"),
    "espanha":  ("Espanha",  "es"),
    "israel":   ("Israel",   "il"),
    "marrocos": ("Marrocos", "ma"),
    "peru":     ("Peru",     "pe"),
}

# Nome do país em inglês, para montar as queries
EN = {
    "brasil": "Brazil", "chile": "Chile", "colombia": "Colombia",
    "espanha": "Spain", "israel": "Israel", "marrocos": "Morocco", "peru": "Peru",
}

# Par de câmbio por país. O Brasil é a moeda de quem lê o dashboard; os outros
# são a moeda do produtor local.
PARES = {
    "brasil":   ("USD/BRL", "BRL=X"),
    "chile":    ("USD/CLP", "CLP=X"),
    "colombia": ("USD/COP", "COP=X"),
    "espanha":  ("USD/EUR", "EUR=X"),
    "israel":   ("USD/ILS", "ILS=X"),
    "marrocos": ("USD/MAD", "MAD=X"),
    "peru":     ("USD/PEN", "PEN=X"),
}

# As queries dos scripts antigos, consolidadas. `{p}` é o país em inglês.
QUERIES = {
    "geral": [
        "avocado {p}",
        "avocado export {p}",
        "{p} avocado production",
        "{p} agriculture market",
        "{p} agriculture investment",
    ],
    "logistica": [
        "{p} port logistics",
        "{p} shipping delay",
        "{p} container freight export",
        "{p} cold chain fruit export",
    ],
    "regulacao": [
        "{p} export regulation agriculture",
        "{p} phytosanitary export",
        "{p} customs export rules",
        "{p} agricultural trade policy",
    ],
}

# Impacto por categoria — mesma convenção dos scripts antigos, que gravavam
# Alto em regulação/logística e Médio em geral.
IMPACTO = {"geral": "Médio", "logistica": "Alto", "regulacao": "Alto"}

MAX_TOPICOS = 6
MAX_CHARS = 180
SERP_ENDPOINT = "https://serpapi.com/search.json"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELO = os.environ.get("ANTHROPIC_MODELO", "claude-sonnet-5")

AVISOS: list[str] = []


def aviso(msg: str) -> None:
    """Aviso identico nao repete: o de chave ausente saia uma vez por
    pais x categoria, 21 linhas dizendo a mesma coisa."""
    if msg in AVISOS:
        return
    AVISOS.append(msg)
    print(f"    ! {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# COLETA
# ─────────────────────────────────────────────────────────────────────────────

def corta(t: str | None) -> str:
    t = (t or "").strip()
    if len(t) > MAX_CHARS:
        return t[: MAX_CHARS - 3] + "..."
    return t


def busca(query: str, gl: str, chave: str) -> list[dict]:
    """Uma query no google_news. Erro de rede não derruba o país inteiro."""
    try:
        r = requests.get(
            SERP_ENDPOINT,
            params={"engine": "google_news", "q": query, "hl": "en",
                    "gl": gl, "api_key": chave},
            timeout=40,
        )
    except requests.RequestException as e:
        aviso(f"query '{query}': {type(e).__name__}")
        return []
    if r.status_code != 200:
        aviso(f"query '{query}': HTTP {r.status_code}")
        return []
    return r.json().get("news_results", []) or []


def classifica_tag(texto: str, categoria: str) -> str:
    """
    Só a categoria `geral` precisa de classificação: regulação e logística já
    são o próprio recorte da busca. Mantém as regras dos scripts antigos.
    """
    if categoria != "geral":
        return {"logistica": "logística", "regulacao": "regulação"}[categoria]
    t = (texto or "").lower()
    if any(p in t for p in ("export", "shipping", "trade")):
        return "exportação"
    if any(p in t for p in ("price", "market", "demand")):
        return "mercado"
    if any(p in t for p in ("investment", "company", "startup", "expansion")):
        return "negócios"
    if any(p in t for p in ("production", "harvest", "crop")):
        return "produção"
    return "geral"


def traduz(titulos: list[str], chave: str | None) -> list[str]:
    """
    Traduz o lote inteiro numa chamada só — 21 chamadas por rodada em vez de
    126. Devolve os títulos originais se não houver chave, se a resposta não
    vier no formato esperado, ou se a contagem não casar: título em inglês é
    degradação aceitável, título trocado de notícia não é.
    """
    if not titulos:
        return []
    if not chave:
        aviso("ANTHROPIC_API_KEY ausente — manchetes ficam no idioma original")
        return titulos

    prompt = (
        "Traduza para português do Brasil, de forma jornalística e objetiva, "
        "cada manchete da lista abaixo. Responda APENAS com um array JSON de "
        "strings, na mesma ordem e com o mesmo número de itens, sem nenhum "
        "texto em volta.\n\n" + json.dumps(titulos, ensure_ascii=False, indent=1)
    )
    corpo = json.dumps({
        "model": ANTHROPIC_MODELO,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_ENDPOINT, data=corpo, method="POST",
        headers={"content-type": "application/json", "x-api-key": chave,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        txt = "".join(b.get("text", "") for b in data.get("content", []))
        i, j = txt.find("["), txt.rfind("]")
        out = json.loads(txt[i:j + 1])
        if not isinstance(out, list) or len(out) != len(titulos):
            aviso(f"tradução devolveu {len(out)} itens para {len(titulos)} títulos — mantive os originais")
            return titulos
        return [str(x).strip() or titulos[k] for k, x in enumerate(out)]
    except Exception as e:
        aviso(f"tradução falhou ({type(e).__name__}) — manchetes no idioma original")
        return titulos


def urls_existentes(pais: str, categoria: str, data: str) -> set[str]:
    """
    URLs já gravadas para esse (país, categoria, data). É o que impede a mesma
    notícia de entrar duas vezes com traduções diferentes.
    """
    base = supabase_upsert.SUPABASE_URL.rstrip("/")
    q = urllib.parse.urlencode({
        "select": "fonte_url",
        "pais": f"eq.{pais}",
        "categoria": f"eq.{categoria}",
        "data": f"eq.{data}",
    })
    req = urllib.request.Request(
        f"{base}/rest/v1/news?{q}",
        headers={"apikey": supabase_upsert.SUPABASE_KEY,
                 "Authorization": f"Bearer {supabase_upsert.SUPABASE_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {r["fonte_url"] for r in json.load(resp) if r.get("fonte_url")}
    except Exception as e:
        aviso(f"não consegui ler URLs já gravadas de {pais}/{categoria} ({type(e).__name__}) — "
              "sigo sem dedupe por URL")
        return set()


def coleta_pais(slug: str, categoria: str, data: str, serp: str,
                anth: str | None, dry: bool, etapa: str = "") -> list[dict]:
    pais, gl = PAISES[slug]
    qs = QUERIES[categoria]
    t0 = time.time()
    brutos: list[dict] = []
    for i, q in enumerate(qs, 1):
        # sem isto a tela fica parada minutos: sao ~90 chamadas em serie e a
        # unica pista era o nome do pais, impresso DEPOIS de terminar
        print(f"    {etapa} {pais} / {categoria}  query {i}/{len(qs)}...",
              end="\r", flush=True)
        brutos.extend(busca(q.format(p=EN[slug]), gl, serp))
        time.sleep(0.4)

    vistos = set() if dry else urls_existentes(pais, categoria, data)
    novos: list[dict] = []
    for n in brutos:
        titulo_en = corta(n.get("title"))
        link = n.get("link")
        if not titulo_en or not link:
            continue
        if link in vistos:
            continue
        vistos.add(link)
        novos.append({"titulo_en": titulo_en, "link": link})
        if len(novos) >= MAX_TOPICOS:
            break

    traduzidos = traduz([x["titulo_en"] for x in novos], anth)
    linhas = []
    for x, pt in zip(novos, traduzidos):
        tag = classifica_tag(x["titulo_en"], categoria)
        linhas.append({
            "data": data,
            "pais": pais,
            "categoria": categoria,
            "tag": tag,
            "impacto": IMPACTO[categoria],
            "estado": pais,
            "texto": f"{pt} [{tag}]",
            "fonte_url": x["link"],
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        })
    print(f"    {etapa} {pais:10s} {categoria:10s} {len(brutos):4d} resultados -> "
          f"{len(linhas)} novos   ({time.time()-t0:.0f}s)")
    return linhas


def coleta_cambio(slugs: list[str], data: str) -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        aviso("yfinance não instalado — câmbio não coletado")
        return []

    linhas = []
    for slug in slugs:
        par, ticker = PARES[slug]
        try:
            h = yf.Ticker(ticker).history(period="1d")
            if h.empty:
                aviso(f"{par}: sem cotação hoje ({ticker})")
                continue
            valor = float(h["Close"].iloc[-1])
        except Exception as e:
            aviso(f"{par}: {type(e).__name__} ({ticker})")
            continue
        if not (0 < valor < 100000):
            aviso(f"{par}: valor fora de faixa ({valor}) — descartado")
            continue
        linhas.append({"data": data, "par": par, "valor": round(valor, 6),
                       "fonte": f"yfinance {ticker}",
                       "extracted_at": datetime.now(timezone.utc).isoformat()})
        print(f"    {par:10s} {valor:>12.4f}")
    return linhas


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def arg(nome: str, default: str | None = None) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(nome + "="):
            return a.split("=", 1)[1]
        if a == nome:
            i = sys.argv.index(a)
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                return sys.argv[i + 1]
    return default


def main() -> int:
    dry = "--dry-run" in sys.argv
    sem_cambio = "--sem-cambio" in sys.argv
    so_cambio = "--so-cambio" in sys.argv
    sem_trad = "--sem-traducao" in sys.argv

    slugs = list(PAISES)
    p = arg("--pais")
    if p:
        slugs = [s.strip().lower() for s in p.split(",")]
        ruins = [s for s in slugs if s not in PAISES]
        if ruins:
            print(f"ERRO: país desconhecido: {', '.join(ruins)}")
            print(f"       disponíveis: {', '.join(PAISES)}")
            return 2

    cats = list(QUERIES)
    c = arg("--categoria")
    if c:
        cats = [s.strip().lower() for s in c.split(",")]
        ruins = [s for s in cats if s not in QUERIES]
        if ruins:
            print(f"ERRO: categoria desconhecida: {', '.join(ruins)}")
            print(f"       disponíveis: {', '.join(QUERIES)}")
            return 2

    serp = os.environ.get("SERPAPI_KEY", "")
    anth = None if sem_trad else os.environ.get("ANTHROPIC_API_KEY")
    data = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 62)
    print("ETL NEWS — BI Avocado TFruits")
    print(f"data {data} · {len(slugs)} países · {len(cats)} categorias"
          + (" · DRY-RUN" if dry else ""))
    print("=" * 62)

    linhas_news: list[dict] = []
    if not so_cambio:
        if not serp:
            print("ERRO: SERPAPI_KEY não configurada — sem ela não há coleta de notícias.")
            return 1
        total = len(slugs) * len(cats)
        print(f"\nNotícias ({total} coletas, ~{total * 5} chamadas à SerpAPI — "
              f"conte alguns minutos):")
        feito = 0
        try:
            for slug in slugs:
                for cat in cats:
                    feito += 1
                    linhas_news.extend(
                        coleta_pais(slug, cat, data, serp, anth, dry,
                                    etapa=f"[{feito:2d}/{total}]"))
        except KeyboardInterrupt:
            # interrompido depois de minutos de coleta: o que ja veio e bom e o
            # upsert e idempotente, entao grava em vez de descartar
            print(f"\n  interrompido em {feito}/{total} — gravando o que já veio")
            sem_cambio = True

    linhas_cambio: list[dict] = []
    if not sem_cambio:
        print("\nCâmbio:")
        linhas_cambio = coleta_cambio(slugs, data)

    print(f"\nTotal: {len(linhas_news)} notícias · {len(linhas_cambio)} cotações")

    if dry:
        for l in linhas_news[:5]:
            print(f"  [{l['pais']}/{l['categoria']}] {l['texto'][:90]}")
        if len(linhas_news) > 5:
            print(f"  ... e {len(linhas_news) - 5} outras")
        print("  --dry-run: nada foi gravado.")
        return 0

    erros = []
    if linhas_news:
        r = supabase_upsert.upsert("news", linhas_news,
                                   on_conflict="pais,categoria,data,texto_hash")
        print(f"news:   {r['inserted']} registros")
        erros += r.get("errors", [])
    if linhas_cambio:
        r = supabase_upsert.upsert("cambio", linhas_cambio, on_conflict="data,par")
        print(f"cambio: {r['inserted']} registros")
        erros += r.get("errors", [])

    if AVISOS:
        print(f"\nAvisos ({len(AVISOS)}):")
        for a in AVISOS:
            print(f"  - {a}")

    if erros:
        print(f"\nERROS ({len(erros)}):")
        for e in erros[:10]:
            print(f"  - {e}")
        return 1

    # Nenhuma notícia nova não é falha: a fonte pode simplesmente não ter
    # publicado nada novo desde a última rodada.
    if not linhas_news and not linhas_cambio and not so_cambio:
        print("\nNada novo nesta rodada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
