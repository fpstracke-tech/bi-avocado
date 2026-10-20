#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
De onde vem o PDF semanal da CIRAD.

O boletim chega por e-mail. Um flow do Power Automate (conectores padrão,
sem premium) pega o anexo da mensagem e grava numa pasta do SharePoint. Este
módulo lê essa pasta pelo Microsoft Graph com um app do Entra que só enxerga
aquele site (permissão Sites.Selected), então o job do Actions não depende de
nenhuma conta de usuário nem de token que expira quando a senha muda.

Ordem das fontes:
  1. SharePoint (Graph)  se M365_TENANT_ID / M365_CLIENT_ID / M365_CLIENT_SECRET
                         / M365_SITE / M365_PASTA estiverem no ambiente
  2. ownCloud (WebDAV)   se OWNCLOUD_* estiverem — é onde está o acervo antigo

As duas devolvem a mesma coisa: lista de Boletim(nome, quando, baixar), do
mais novo para o mais velho. `baixar()` devolve (nome, BytesIO).

    M365_SITE   "tfruitscombr.sharepoint.com:/sites/TFruits"   (host:/caminho do site)
    M365_PASTA  "TFruits PowerBI:/BI Avocado/CIRAD"            (biblioteca:/caminho)
                "BI Avocado/CIRAD"                             (sem ":/" = biblioteca padrão)
"""
from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from xml.etree import ElementTree

import requests

try:                      # rodando local, o .env entra pelo mesmo loader dos ETLs
    import supabase_upsert  # noqa: F401
except Exception:         # noqa: BLE001
    pass

GRAPH = "https://graph.microsoft.com/v1.0"
TRANSITORIOS = {408, 429, 500, 502, 503, 504}


@dataclass
class Boletim:
    nome: str
    quando: datetime | None
    baixar: Callable[[], tuple[str, io.BytesIO]]
    origem: str


def _env(*nomes: str) -> dict:
    return {n: (os.environ.get(n) or "").strip() for n in nomes}


def _req(metodo: str, url: str, tentativas: int = 4, **kw) -> requests.Response:
    """Retry só em erro transitório; 401/403/404 sobem na primeira."""
    ultimo = None
    for t in range(tentativas):
        try:
            r = requests.request(metodo, url, timeout=kw.pop("timeout", 120), **kw)
        except requests.RequestException as e:
            ultimo = e
            time.sleep(2 ** t)
            continue
        if r.status_code in TRANSITORIOS:
            espera = int(r.headers.get("Retry-After") or 2 ** t)
            ultimo = requests.HTTPError(f"HTTP {r.status_code}", response=r)
            time.sleep(min(espera, 30))
            continue
        r.raise_for_status()
        return r
    raise ultimo  # type: ignore[misc]


# ── SHAREPOINT VIA GRAPH ──────────────────────────────────────────────────────

M365_VARS = ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET",
             "M365_SITE", "M365_PASTA")


def sharepoint_configurado() -> bool:
    return all(_env(*M365_VARS).values())


def _token(cfg: dict) -> str:
    r = _req("POST",
             f"https://login.microsoftonline.com/{cfg['M365_TENANT_ID']}/oauth2/v2.0/token",
             data={"grant_type": "client_credentials",
                   "client_id": cfg["M365_CLIENT_ID"],
                   "client_secret": cfg["M365_CLIENT_SECRET"],
                   "scope": "https://graph.microsoft.com/.default"})
    return r.json()["access_token"]


def _quando(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _drive_e_pasta(site: str, pasta_cfg: str, h: dict) -> tuple[str, str]:
    """
    "Biblioteca:/caminho" -> (drive da biblioteca pelo nome, caminho)
    "caminho"             -> (drive padrão do site, caminho)
    """
    if ":/" in pasta_cfg:
        biblioteca, caminho = pasta_cfg.split(":/", 1)
        biblioteca = biblioteca.strip()
        drives = _req("GET", f"{GRAPH}/sites/{site}/drives?$select=id,name,webUrl",
                      headers=h).json().get("value", [])
        alvo = biblioteca.lower()
        for d in drives:
            url_final = requests.utils.unquote((d.get("webUrl") or "").rstrip("/").split("/")[-1])
            if (d.get("name") or "").lower() == alvo or url_final.lower() == alvo:
                return d["id"], caminho.strip("/")
        nomes = ", ".join(sorted(d.get("name") or "?" for d in drives))
        raise LookupError(f"biblioteca '{biblioteca}' não existe no site (há: {nomes})")

    drive = _req("GET", f"{GRAPH}/sites/{site}/drive?$select=id", headers=h).json()["id"]
    return drive, pasta_cfg.strip("/")


def listar_sharepoint() -> list[Boletim]:
    cfg = _env(*M365_VARS)
    h = {"Authorization": f"Bearer {_token(cfg)}"}

    site = _req("GET", f"{GRAPH}/sites/{cfg['M365_SITE']}", headers=h).json()["id"]
    drive, caminho = _drive_e_pasta(site, cfg["M365_PASTA"], h)
    pasta = requests.utils.quote(caminho)
    url = (f"{GRAPH}/drives/{drive}/root:/{pasta}:/children"
           f"?$select=id,name,lastModifiedDateTime,file&$top=200")

    itens = []
    while url:
        j = _req("GET", url, headers=h).json()
        itens += j.get("value", [])
        url = j.get("@odata.nextLink")

    saida = []
    for it in itens:
        nome = it.get("name") or ""
        if "file" not in it or not nome.lower().endswith(".pdf"):
            continue

        def baixar(item_id=it["id"], nome=nome):
            r = _req("GET", f"{GRAPH}/drives/{drive}/items/{item_id}/content",
                     headers=h, timeout=300)
            if not r.content.startswith(b"%PDF"):
                raise ValueError(f"{nome}: conteúdo não é PDF")
            return nome, io.BytesIO(r.content)

        saida.append(Boletim(nome, _quando(it.get("lastModifiedDateTime")),
                             baixar, "SharePoint"))

    saida.sort(key=lambda b: (b.quando or datetime.min.replace(tzinfo=timezone.utc),
                              b.nome), reverse=True)
    print(f"  SharePoint: {len(saida)} PDFs em {cfg['M365_PASTA']}", flush=True)
    return saida


# ── OWNCLOUD VIA WEBDAV ───────────────────────────────────────────────────────

OC_VARS = ("OWNCLOUD_URL", "OWNCLOUD_PASTA", "OWNCLOUD_USER", "OWNCLOUD_PASS")


def owncloud_configurado() -> bool:
    return all(_env(*OC_VARS).values())


def listar_owncloud() -> list[Boletim]:
    from email.utils import parsedate_to_datetime

    cfg = _env(*OC_VARS)
    auth = (cfg["OWNCLOUD_USER"], cfg["OWNCLOUD_PASS"])
    alvo = f"{cfg['OWNCLOUD_URL'].rstrip('/')}/{requests.utils.quote(cfg['OWNCLOUD_PASTA'].strip('/'))}/"
    corpo = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
             '<d:prop><d:getlastmodified/></d:prop></d:propfind>')
    r = _req("PROPFIND", alvo, auth=auth, data=corpo,
             headers={"Depth": "1", "Content-Type": "application/xml"})
    raiz = ElementTree.fromstring(r.content)

    saida = []
    for resp in raiz.findall("{DAV:}response"):
        href = requests.utils.unquote(resp.findtext("{DAV:}href") or "")
        if not href.lower().endswith(".pdf"):
            continue
        nome = href.rstrip("/").split("/")[-1]
        mod = resp.findtext(".//{DAV:}getlastmodified")
        try:
            quando = parsedate_to_datetime(mod) if mod else None
        except (TypeError, ValueError):
            quando = None

        def baixar(nome=nome):
            rr = _req("GET", f"{alvo}{requests.utils.quote(nome)}", auth=auth,
                      timeout=300)
            return nome, io.BytesIO(rr.content)

        saida.append(Boletim(nome, quando, baixar, "ownCloud"))

    saida.sort(key=lambda b: (b.quando or datetime.min.replace(tzinfo=timezone.utc),
                              b.nome), reverse=True)
    print(f"  ownCloud: {len(saida)} PDFs na pasta", flush=True)
    return saida


# ── ORQUESTRAÇÃO ──────────────────────────────────────────────────────────────

def listar_boletins() -> list[Boletim]:
    """
    SharePoint primeiro (onde o flow grava as edições novas); ownCloud entra
    junto se estiver configurado, porque lá está o acervo 2023–S36/2026.
    Mesmo nome nas duas: vale o do SharePoint.
    """
    fontes = []
    if sharepoint_configurado():
        fontes.append(listar_sharepoint)
    if owncloud_configurado():
        fontes.append(listar_owncloud)
    if not fontes:
        raise EnvironmentError(
            "Nenhuma fonte de boletim configurada. Defina M365_* (SharePoint) "
            "ou OWNCLOUD_* (WebDAV).")

    vistos, saida, erros = set(), [], []
    for f in fontes:
        try:
            lote = f()
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{f.__name__}: {str(e)[:200]}")
            print(f"  AVISO: {f.__name__} falhou: {str(e)[:200]}", flush=True)
            continue
        for b in lote:
            chave = b.nome.lower()
            if chave not in vistos:
                vistos.add(chave)
                saida.append(b)

    if not saida and erros:
        raise RuntimeError("todas as fontes de boletim falharam: " + " | ".join(erros))
    saida.sort(key=lambda b: (b.quando or datetime.min.replace(tzinfo=timezone.utc),
                              b.nome), reverse=True)
    return saida
