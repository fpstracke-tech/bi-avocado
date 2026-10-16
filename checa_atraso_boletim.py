#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alarme de boletim CIRAD atrasado.

O ETL Europa só sabe falhar quando um PDF existe e não dá para ler. Quando o
boletim simplesmente PARA de chegar (aconteceu de S37 a S42/2026: a CIRAD
deixou de enviar e ninguém percebeu por seis semanas), o job continua verde
todo dia, processando a mesma edição velha. Este script fecha esse buraco.

Compara a última semana em europa_cirad_precos com a semana corrente
(weeknum Excel, domingo como início, a mesma convenção do banco). O boletim
da semana N sai na sexta da semana N com o preço de N ou N-1, então até 2
semanas de distância é normal. A partir de 3, exit 2 e o workflow abre issue.

    python checa_atraso_boletim.py            # exit 0 ok, 2 atrasado, 1 erro
    python checa_atraso_boletim.py --limite 3
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import supabase_upsert


def weeknum_excel(d: date) -> tuple[int, int]:
    """WEEKNUM(d, 1): semana 1 contém 1º de janeiro, semana começa no domingo."""
    jan1 = date(d.year, 1, 1)
    inicio = jan1 - timedelta(days=(jan1.weekday() + 1) % 7)   # domingo anterior
    return d.year, (d - inicio).days // 7 + 1


def semanas_entre(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Distância em semanas de (ano, sem) a até b, cruzando virada de ano."""
    def domingo(ano, sem):
        jan1 = date(ano, 1, 1)
        return jan1 - timedelta(days=(jan1.weekday() + 1) % 7) + timedelta(weeks=sem - 1)
    return (domingo(*b) - domingo(*a)).days // 7


def main() -> int:
    limite = 3
    if "--limite" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--limite") + 1])

    st = supabase_upsert.select("v_europa_cirad_status", {"select": "*"})
    if not st or not st[0].get("ultima"):
        print("  ERRO: não consegui ler v_europa_cirad_status")
        return 1

    ultima = int(st[0]["ultima"])                    # ex. 202636
    u = (ultima // 100, ultima % 100)
    hoje = weeknum_excel(date.today())
    atraso = semanas_entre(u, hoje)

    print(f"  último preço no banco: S{u[1]:02d}/{u[0]} · semana corrente: "
          f"S{hoje[1]:02d}/{hoje[0]} · distância: {atraso} semana(s)")

    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as f:
            f.write(f"ultima=S{u[1]:02d}/{u[0]}\natraso={atraso}\n")

    if atraso >= limite:
        print(f"  ATRASADO: {atraso} semanas sem boletim novo (limite {limite}).")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
