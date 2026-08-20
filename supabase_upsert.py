"""
supabase_upsert.py — Helper de upsert para o Supabase
======================================================
BI Avocado — TFruits. Copia fiel do helper do BI Limao, para os dois
projetos terem o mesmo contrato de ETL.

Usado por todos os scripts ETL para enviar dados ao Supabase
via REST API (sem SDK, so requests).

Configuração:
    Defina as variáveis de ambiente antes de rodar:
        set SUPABASE_URL=https://xxxxxxxxxxx.supabase.co
        set SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

    Ou crie um arquivo .env na mesma pasta:
        SUPABASE_URL=https://xxxxxxxxxxx.supabase.co
        SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
"""

import os
import json
import time
import requests
from pathlib import Path

# ── CARREGAR CONFIG ────────────────────────────────────────────────────────────
def _load_env():
    """Carrega .env local se existir (sem depender de python-dotenv)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()  # força sobrescrita

_load_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key


def _base_headers(prefer: str = "resolution=merge-duplicates") -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        prefer,
    }


def _safe_batch(batch: list[dict]) -> list[dict]:
    """
    Serializa valores não-JSON-nativos (datetime, NaN) e NORMALIZA AS CHAVES.

    O PostgREST exige que todos os objetos de um lote tenham exatamente o mesmo
    conjunto de chaves; se um dicionário traz um campo que outro não traz, ele
    devolve 400 com:

        {"code":"PGRST102","message":"All object keys must match"}

    Aconteceu em 12/08/2026 no ETL da Europa: as linhas vindas da tabela do PDF
    tinham variacao_eur e variacao_media_pct, as vindas do gráfico não. Como é um
    erro que qualquer ETL pode cometer sem perceber, o conserto fica aqui: uso a
    UNIÃO das chaves do lote e preencho o que falta com None (que o Postgres
    grava como NULL, o valor honesto para "esse dado não veio").
    """
    todas = set()
    for row in batch:
        todas.update(row.keys())

    result = []
    for row in batch:
        safe_row = {}
        for k in todas:
            v = row.get(k)
            if hasattr(v, "isoformat"):
                safe_row[k] = v.isoformat()
            elif isinstance(v, float) and v != v:      # NaN
                safe_row[k] = None
            else:
                safe_row[k] = v
        result.append(safe_row)
    return result


# ── UPSERT ─────────────────────────────────────────────────────────────────────
def upsert(table: str, records: list[dict], batch_size: int = 500,
           on_conflict: str | None = None) -> dict:
    """
    Faz upsert de registros no Supabase (INSERT ... ON CONFLICT DO UPDATE).
    Depende da constraint UNIQUE definida no schema.

    Args:
        table:       nome da tabela (ex: "brasil_precos")
        records:     lista de dicts com os dados
        batch_size:  registros por request (padrão 500)
        on_conflict: colunas de conflito separadas por virgula (ex: "data,mercado,produto")
                     Se None, usa a constraint UNIQUE padrão da tabela.

    Returns:
        {"inserted": N, "errors": [...]}
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError(
            "SUPABASE_URL e SUPABASE_KEY não configurados.\n"
            "Crie um arquivo .env na pasta do projeto com:\n"
            "  SUPABASE_URL=https://xxxx.supabase.co\n"
            "  SUPABASE_KEY=eyJ..."
        )

    base_url = f"{SUPABASE_URL}/rest/v1/{table}"
    params   = {"on_conflict": on_conflict} if on_conflict else {}
    headers  = _base_headers("resolution=merge-duplicates")
    total    = 0
    errors   = []

    # Falhas transitórias observadas em produção (11/08/2026): o job da
    # Argentina no GitHub Actions tomou
    #   HTTP 401 {"code":"PGRST303","message":"JWT issued at future"}
    # enquanto Chile e Uruguai, com a MESMA chave e nos mesmos minutos,
    # gravaram. PGRST303 é desvio de relógio entre quem emite o token e o
    # PostgREST — não é chave inválida, e uma nova tentativa resolve.
    # Sem retry, meia hora de trabalho de scraping era jogada fora por um
    # segundo de skew.
    TRANSITORIOS = {408, 429, 500, 502, 503, 504}

    def _transitorio(resp) -> bool:
        if resp.status_code in TRANSITORIOS:
            return True
        if resp.status_code == 401 and "PGRST303" in (resp.text or ""):
            return True
        return False

    for i in range(0, len(records), batch_size):
        batch = _safe_batch(records[i : i + batch_size])
        ultima = None
        for tentativa in range(4):
            try:
                r = requests.post(base_url, headers=headers, params=params,
                                  data=json.dumps(batch), timeout=60)
            except requests.RequestException as e:
                ultima = {"batch_start": i, "status": "rede", "detail": str(e)[:300]}
                time.sleep(2 ** tentativa)
                continue

            if r.status_code in (200, 201):
                total += len(batch)
                ultima = None
                break

            ultima = {"batch_start": i, "status": r.status_code,
                      "detail": r.text[:300]}
            if not _transitorio(r):
                break                      # erro real: não insiste
            espera = 2 ** tentativa         # 1s, 2s, 4s, 8s
            print(f"    lote {i}: HTTP {r.status_code} transitório, "
                  f"nova tentativa em {espera}s", flush=True)
            time.sleep(espera)

        if ultima:
            errors.append(ultima)

    return {"inserted": total, "errors": errors}


# ── SELECT ─────────────────────────────────────────────────────────────────────
def select(table: str, params: dict | None = None, tentativas: int = 4) -> list[dict]:
    """
    Leitura via PostgREST. Devolve lista de dicts, ou [] se não der para ler.

    Existe porque o banco é a fonte mais confiável que os ETLs têm à mão: o que
    já foi gravado uma vez não depende de nenhum site continuar no ar. O caso de
    uso que motivou (20/08/2026) é o câmbio — `precos_origem.cotacao_local`
    guarda a taxa de cada dia, então uma recarga do ano inteiro não precisa
    pedir de novo ao mindicador.cl aquilo que já está no banco.

    NUNCA levanta exceção: quem chama trata [] como "não tenho cache", não como
    erro fatal. Um ETL não pode morrer porque o cache não respondeu.

        select("precos_origem", {
            "select": "data,cotacao_local",
            "pais":   "eq.Chile",
            "data":   "gte.2026-01-01",
            "limit":  "10000",
        })
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("    select: SUPABASE_URL/KEY ausentes, seguindo sem cache", flush=True)
        return []

    url     = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept":        "application/json",
    }
    TRANSITORIOS = {408, 429, 500, 502, 503, 504}

    for tentativa in range(tentativas):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=60)
        except requests.RequestException as e:
            print(f"    select {table}: rede falhou ({str(e)[:120]})", flush=True)
            time.sleep(2 ** tentativa)
            continue

        if r.status_code == 200:
            try:
                dados = r.json()
            except ValueError:
                print(f"    select {table}: resposta não é JSON", flush=True)
                return []
            return dados if isinstance(dados, list) else []

        transitorio = (r.status_code in TRANSITORIOS
                       or (r.status_code == 401 and "PGRST303" in (r.text or "")))
        if not transitorio:
            print(f"    select {table}: HTTP {r.status_code} — {r.text[:200]}",
                  flush=True)
            return []
        time.sleep(2 ** tentativa)

    print(f"    select {table}: sem resposta em {tentativas} tentativas", flush=True)
    return []


def insert(table: str, records: list[dict], batch_size: int = 500) -> dict:
    """
    Insert simples (sem upsert) — para tabelas sem UNIQUE constraint.
    Usa Prefer: return=minimal para não retornar dados.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_URL e SUPABASE_KEY não configurados.")

    url     = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = _base_headers("return=minimal")
    total   = 0
    errors  = []

    for i in range(0, len(records), batch_size):
        batch = _safe_batch(records[i : i + batch_size])
        r = requests.post(url, headers=headers, data=json.dumps(batch), timeout=30)
        if r.status_code in (200, 201):
            total += len(batch)
        else:
            errors.append({"batch_start": i, "status": r.status_code, "detail": r.text[:300]})

    return {"inserted": total, "errors": errors}


def delete_old(table: str, column: str, keep_latest_n_days: int = 30):
    """
    Remove registros mais antigos que N dias (útil para clima/forecast).
    Apenas para tabelas sem UNIQUE por data (clima_brasil_atual, clima_forecast).
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }
    cutoff = f"now() - interval '{keep_latest_n_days} days'"
    params = {column: f"lt.{cutoff}"}
    r = requests.delete(url, headers=headers, params=params, timeout=30)
    return r.status_code


# ── TESTE RÁPIDO ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SUPABASE_URL:
        print("⚠️  Configure SUPABASE_URL e SUPABASE_KEY no arquivo .env")
    else:
        print(f"✅ Supabase configurado: {SUPABASE_URL}")
        print(f"   Key (primeiros 20 chars): {SUPABASE_KEY[:20]}...")
        # Testar conexão lendo a view de status
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/v_ultima_atualizacao",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=10,
        )
        if r.status_code == 200:
            print("✅ Conexão OK. Status das tabelas:")
            for row in r.json():
                print(f"   {row['tabela']:30s} | {row['total_registros']:6} registros | {row['ultima_atualizacao']}")
        else:
            print(f"❌ Erro: {r.status_code} — {r.text[:200]}")
