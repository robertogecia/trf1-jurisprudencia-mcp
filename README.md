# MCP — Jurisprudência do TRF1 (portal do CJF)

Servidor MCP pessoal que pesquisa a jurisprudência do **TRF1** e das **Turmas Recursais/JEF1**
no portal oficial do Conselho da Justiça Federal (`https://jurisprudencia.cjf.jus.br/trf1`),
sem login. Irmão do `~/MCP/tjro-jurisprudencia` — mesma disciplina (disjuntor compartilhado
entre processos, detecção de bloqueio, citação pronta, id do documento), protocolo diferente.

Criado em 11/09/2026. Spec do protocolo em `references/protocolo-cjf.md`; o que foi portado do
TJRO em `references/arquitetura-tjro.md`; respostas cruas do portal em `fixtures/`.

## Ferramentas

| Tool | O que faz |
|---|---|
| `buscar_jurisprudencia_trf1` | busca por tema na sintaxe do motor (E/OU/NAO/ADJ/PROX/COM/MESMO/`$`/`[CAMPO]`), `grupos` de sinônimos montados pela ferramenta, filtros do painel avançado (relator, órgão, classe, origem, número, ementa/decisão, ref. legislativa, período por julgamento ou publicação), paginação 10/30/50 |
| `obter_decisao_trf1` | todos os documentos publicados sob um número (acórdão, embargos, monocrática), com ementa e dispositivo **integrais** — o substituto do "inteiro teor" no TRF1; na base `tnu` traz também o **inteiro teor** (relatório + voto) baixado do eproc da TNU, até 3 por chamada |
| `verificar_citacao_trf1` | confere se um trecho aparece literalmente na ementa/dispositivo (e inteiro teor, na TNU) antes de ir entre aspas — `[...]` separa fragmentos em ordem; ❌ vem com o fragmento que não bateu |
| `diagnostico_ritmo_trf1` | estado do limitador e histórico de bloqueios, sem rede |

**Bases** (parâmetro `base`, mesmo portal, mapeadas em 11/09/2026): `trf1` (padrão — TRF1 + JEF1, painel avançado,
voto não exposto); `tnu` (Turma Nacional de Uniformização — tipos ACORDAO/DECISAOMONO/DECISAOPRES, filtro
`tipo_acordao` = REPRESENTATIVO / RELEVANTE para precedentes qualificados, que saem com ★ e entram na citação;
inteiro teor em HTML sem desafio → "inteiro teor lido" possível); `colegiado` (decisões ADMINISTRATIVAS do
Conselho — procedimentos normativos, inspeções; inteiro teor embutido no resultado; raramente serve a litígio).
Nas bases `tnu`/`colegiado` não há painel avançado: filtro vai na sintaxe de campo da consulta (`nome[REL]`).

Cada resultado traz: tipo, classe, número, **id do documento** (chave única — sob o mesmo número
convivem várias decisões), relator (+ convocado / para acórdão), órgão, datas, fonte, citação no
padrão `(TRF-1 - AC: nº, Relator: …, Data de Julgamento: …, TURMA, Data de Publicação: …)` —
com a **referência inteira em hiperlink** para o inteiro teor quando o portal deu um link
específico deste documento (`arquivo.trf1.jus.br`, eproc da TNU), nunca no link genérico do
PJe (mesmo href em todo resultado PJe — hiperlinkar ali apontaria pro lugar errado). Mesma
convenção que a skill `peticao-rg` já aplica para o TJRO desde 14/09/2026 ("a referência
inteira entre parênteses vira link clicável"), só que aqui na origem, com o link que a
própria busca encontrou — ementa (trecho de 800 chars na busca; integral em
`obter_decisao_trf1`), dispositivo, e a nota de inteiro teor.

## Limite estrutural — dito sem rodeio

Na base `trf1`, o portal entrega **ementa e dispositivo, nunca o voto.** O inteiro teor dos casos antigos está em
`arquivo.trf1.jus.br`, atrás de desafio Cloudflare (403 já na 1ª requisição, 11/09/2026) — este
servidor **não** tenta passar por ele e não vai tentar. O dos casos PJe é um link genérico à
consulta pública, sem o número do processo. Consequência para a ficha de precedente:
`verificacao: "só ementa/índice"` por padrão; `"inteiro teor lido"` só depois de abrir o PDF no
navegador.

## Como pesquisar bem

A busca casa palavras. Não use preposição, artigo nem pontuação na consulta (o motor não aceita):
`"dano moral" E negativação`, não `dano moral por negativação`. `grupos=[["dano moral"],
["negativação","inscrição indevida"]]` vira `"dano moral" E (negativação OU "inscrição
indevida")` — teste real de 11/09/2026: 1.124 acórdãos. Radical com `$` (`desapropria$`),
campo com `[EMEN]`/`[DECI]`/`[REL]` (na `consulta`, ou no fim de um termo de `grupos`),
ancoragem pela súmula/tema que os julgados citam, e colheita de vocabulário do melhor resultado
via `obter_decisao_trf1`. Sintaxe avançada do motor (`:` de legislação, `{}` de classes, `$[n]`)
só na `consulta` livre — em `grupos` toda pontuação é removida de propósito. O portal **recusa com erro**
os caracteres `# ! + ' ; _ | - @` (mensagem real de 11/09/2026, disparada por "auxílio-doença"): a
ferramenta os troca por espaço em `grupos` e na `consulta`; hífen dentro de palavra vira frase.

Red team adversarial de 11/09/2026 (Opus, offline): 12 achados, todos corrigidos com regressão no
`--selftest` — relatório em `references/red-team-2026-09-11.md`. Hipóteses que só teste online
resolve (total com dois tipos marcados; `por_pagina=50` de fato honrado): registradas lá.

**`por_pagina=50` sem filtro pode estourar o limite de saída de quem chama** (achado real
11/09/2026: "dano moral" sem data/órgão/relator, 50 por página, 88 mil caracteres de resposta) —
use 50 só junto de um filtro que já reduza o total. Nos testes ao vivo do mesmo dia: dois bugs
achados no PRIMEIRO uso real pós-registro (busca vazia com `por_pagina≠30` gastava uma requisição
de paginação à toa; `obter_decisao`+`verificar_citacao` no mesmo número duplicava um aviso por
mutar o dict do cache) — ambos corrigidos com regressão. `colegiado` confirmado: registros
administrativos antigos legitimamente não têm inteiro teor nem dispositivo (não é bug de parser).

## Instalação (pessoal)

```bash
cd ~/MCP/trf1-jurisprudencia
python3 -m venv .venv && .venv/bin/pip install "mcp[cli]>=1.4.0,<2" "httpx>=0.27" "truststore>=0.9"
# mcp<2 de propósito: o 2.x renomeou FastMCP → MCPServer (o registro das tools falha em silêncio)
.venv/bin/python servidor_trf1.py --selftest            # offline, contra os fixtures
.venv/bin/python servidor_trf1.py --selftest --online   # + 3 operações reais (~8 requisições)
```

Registro global em `~/.claude.json` (`mcpServers.trf1_jurisprudencia`), apontando o Python da
venv para `servidor_trf1.py`. O processo MCP só carrega código novo depois de reiniciar o Claude.

## Ritmo e bloqueio

Estado em `.disjuntor_estado_trf1.json` (ao lado do script, sob trava `fcntl`), compartilhado por
todos os processos desta máquina: 24 requisições por janela (cada busca gasta 2 a 4: sessão nova
+ [toggle avançada] + busca [+ paginação]; `obter_decisao_trf1` sempre 4), espaçamento mínimo de 1,5 s, escada de janela que
alarga a cada bloqueio real (1→5→10→20→30 min) e relaxa após 100 sucessos, cooldown de 10 min
dobrando até 1 h. `403/429` seco arma o cooldown sem alargar a escada; `Retry-After` vira piso.

Assinaturas de bloqueio reconhecidas: `<title>Just a moment` / `cf-mitigated` /
`challenges.cloudflare.com` (Cloudflare) e "página bloqueada"/"robotização" (padrão STIC).
**"captcha" genérico foi retirado de propósito** — a página inicial carrega o reCAPTCHA do
"Fale conosco" e isso armou o disjuntor por 10 min num falso positivo real.

## Pontos frágeis (onde olhar se quebrar)

- `name` dos campos do painel avançado (`formulario:j_idtNN`): gerados pelo JSF, mudam num
  redeploy. Lidos ao vivo por posição (12 inputs) com os `<label for>` como conferência;
  fallback em `CAMPOS_AVANCADOS_FALLBACK`. Se um filtro passar a ser ignorado em silêncio, é aqui.
- `formulario:j_idt62` (fonte TRF1/JEF1) e `formulario:tabelaDocumentos` (grid/paginação).
- Cada resultado é `<table class="table_pesquisa_lista" id="doc_N">`, campos em pares
  `label_pontilhada` → valor; o parser itera pelos pares, então rótulo novo não quebra nada.
- Total vem de `rowCount:` do widget DataGrid; fallback nos contadores.

## Não confundir

- Não é o `tjro_jurisprudencia` (portal JURIS, Elasticsearch, sintaxe Lucene, `grupos` com AND/OR).
- Referência de terceiro consultada na concepção: `github.com/fxbarros/MCP-TRF1-Jurisprudencia`
  (Python/FastMCP, mesmo portal). Sem arquivo de licença — serviu de referência de protocolo,
  não de base de código; nada dele é redistribuído aqui.
