#!/usr/bin/env python3
"""Baixa ~25 decisões da TNU com inteiro teor (06/10/2026) para a primeira medição cega PRÓPRIA do TRF1/TNU (alertas e POSIÇÃO).
Um pedido por vez, 20 s entre pedidos; para em duas falhas seguidas. Recibos em ~/.trf1-jurisprudencia-recibos (fora de git)."""
import asyncio, glob, os, re, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import servidor_trf1 as s
TEMAS = ["aposentadoria especial ruído", "benefício assistencial deficiência", "auxílio-doença incapacidade", "pensão por morte dependência",
         "tempo rural início de prova material", "revisão vida toda", "salário-maternidade"]
JA = set()
for f in glob.glob(os.path.expanduser("~/.trf1-jurisprudencia-recibos/*.json")):
    r = json.load(open(f)); JA.add(re.sub(r"\D", "", r.get("nr_processo") or r.get("numero") or ""))
async def main():
    nums = []
    for t in TEMAS:
        r = await s._buscar(t, None, None, None, None, None, None, None, None, None, None, None, None, "julgamento", 1, 10, base="tnu")
        achados = [n for n in dict.fromkeys(re.findall(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b|\b\d{20}\b", r)) if re.sub(r"\D", "", n) not in JA][:4]
        nums += achados; print(t, "→", achados, flush=True); await asyncio.sleep(20)
    nums = list(dict.fromkeys(nums))[:25]
    falhas = 0
    for i, n in enumerate(nums):
        r = await s._obter_decisao(n, "tnu")
        ok = "INTEIRO TEOR" in r.upper() and len(r) > 3000
        print(f"{i + 1}/{len(nums)} {n}: {len(r)} chars {'ok' if ok else 'sem inteiro teor'}", flush=True)
        falhas = 0 if len(r) > 1500 else falhas + 1
        if falhas >= 2: print("duas falhas seguidas — parando"); break
        await asyncio.sleep(20)
asyncio.run(main())
