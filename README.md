# trf1-jurisprudencia-mcp — servidor MCP de jurisprudência do TRF1 e da TNU

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Pesquisa de julgados do **Tribunal Regional Federal da 1ª Região** (TRF1 e Turmas Recursais/JEF1) e da
**Turma Nacional de Uniformização** no portal oficial do Conselho da Justiça Federal, para quem vai **citar em
peça**: busca em linguagem natural ou na sintaxe do motor, ementa e dispositivo integrais, inteiro teor da TNU com
recibo, conferência literal de citação que diz **de quem é a frase**, órgão e data lidos do fecho do acórdão.
Funciona com qualquer cliente MCP (Claude Desktop, Claude Code e outros). Sem login, sem captcha.

Não é produto oficial do TRF1, do CJF nem da TNU. Toda saída é rascunho: quem assina a peça confere.

## Instalar

> **Nunca usou o Terminal? Comece pelo [guia de instalação passo a passo](INSTALAR.md)** — explica onde clicar, o
> que colar e o que cada coisa faz. O resumo abaixo é para quem já tem familiaridade com linha de comando.

```bash
git clone https://github.com/robertogecia/trf1-jurisprudencia-mcp.git trf1-jurisprudencia && cd trf1-jurisprudencia
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python servidor_trf1.py --selftest        # offline, sobre respostas reais do portal guardadas em fixtures/
```

Depois, ligue ao Claude Code:

```bash
claude mcp add trf1_jurisprudencia -- /caminho/trf1-jurisprudencia/.venv/bin/python /caminho/trf1-jurisprudencia/servidor_trf1.py
```

ou ao Claude Desktop, em `claude_desktop_config.json`:

```json
{ "mcpServers": { "trf1_jurisprudencia": {
    "command": "/caminho/trf1-jurisprudencia/.venv/bin/python",
    "args": ["/caminho/trf1-jurisprudencia/servidor_trf1.py"] } } }
```

Não há índice para montar: a busca é **ao vivo** no portal do CJF, e só o inteiro teor da TNU e os recibos ficam
no disco (poucos KB por documento).

**Se não funcionar:**

| Sintoma | O que é |
|---|---|
| As ferramentas não aparecem depois de instalado | Confira a versão do `mcp`: precisa ser `<2`. A série 2.x renomeou `FastMCP` e o registro falha **em silêncio** — o servidor sobe, conecta, e não expõe nada. |
| `[PESQUISA NÃO REALIZADA — …]` | O servidor não conseguiu perguntar ao portal (ritmo esgotado, recusa, rede, timeout). **Nunca é "não localizado"** — é "não perguntei". A própria mensagem diz o motivo e quando tentar de novo; `diagnostico_ritmo_trf1` mostra o estado. |
| A busca deu zero resultado | O motor do CJF casa **palavras**, não conceitos: um grupo com as palavras da conclusão que você espera ("não afasta", "é inócua") zera. Refaça com o FATO julgado (instituto, norma, situação) — veja "Como pesquisar bem". |
| Um filtro do painel avançado passou a ser ignorado | Os `name` dos campos são gerados pelo JSF e mudam num redeploy do portal; o servidor os lê ao vivo por posição com os rótulos como conferência, mas se o painel mudar de forma, é aqui que quebra. Abra uma issue com a resposta do `--selftest --online`. |
| `403` logo na primeira requisição a `arquivo.trf1.jus.br` | Esperado: o arquivo dos casos antigos está atrás de desafio Cloudflare e **este servidor não passa por ele** — o link fica na citação para abrir no navegador. |

## Por que ele é assim

O CJF publica a jurisprudência do TRF1 num portal JSF/PrimeFaces **sem desafio anti-robô na busca**, e a TNU
publica o inteiro teor dos seus acórdãos em HTML aberto no eproc. O servidor fala com esses dois pontos exatamente
como um navegador faria, com User-Agent **honesto** (`trf1-jurisprudencia-mcp/<versão>` com o endereço deste
repositório), ritmo limitado e recuo quando o portal recusa. O que está atrás de desafio (o arquivo dos casos
antigos do TRF1) não é acessado nem contornado — e pedidos para "resolver o captcha" serão recusados.

| Fonte oficial | O que entrega |
|---|---|
| **Portal de jurisprudência do CJF** (`jurisprudencia.cjf.jus.br/trf1`, `/tnu`, `/colegiado`) | ementa e dispositivo integrais, tipo, classe, número, **id do documento**, relator, órgão, datas, fonte de publicação, painel avançado (TRF1) |
| **eproc da TNU** (`eproctnu-jur.cjf.jus.br`) | inteiro teor (relatório, voto, ata) dos acórdãos da TNU, em HTML, sem desafio |

Na base `trf1` o portal entrega **ementa e dispositivo, nunca o voto** — o servidor diz isso em toda resposta em
vez de fingir que leu. Na base `tnu` o inteiro teor vem inteiro, e aí a conferência de citação faz o que faz nos
outros servidores desta família: diz se a frase é do órgão, de outro tribunal transcrito, do voto vencido ou da parte.

## Ferramentas

| Tool | O que faz | Rede |
|---|---|---|
| `buscar_jurisprudencia_trf1` | busca na sintaxe do motor (E/OU/NAO/ADJ/PROX/COM/MESMO/`$`/`[CAMPO]`) ou por `grupos` de sinônimos montados pela ferramenta; filtros do painel avançado (relator, órgão, classe, origem, número, ementa/decisão, ref. legislativa, período por julgamento ou publicação); bases `trf1` / `tnu` / `colegiado`; cada resultado com citação pronta (**hiperlinkada** quando há link específico do documento), nível de verificação e os **sinais** do julgado (súmulas/temas/IRDR citados, ⚠️ monocrática, ⚠️ Turma Recursal) | sim (2–4 req.) |
| `obter_decisao_trf1` | todos os documentos publicados sob um número (acórdão, embargos, monocrática), com ementa e dispositivo **integrais**; na `tnu`, também o **inteiro teor** do eproc, com órgão, relator e data lidos do **fecho** (`orgao_fonte`) e aviso de DIVERGÊNCIA entre índice e texto; grava um **recibo** por documento | sim (4 req.) |
| `verificar_citacao_trf1` | confere se um trecho está **literalmente** na ementa/dispositivo (e no inteiro teor, na TNU) antes de ir entre aspas — por palavra inteira, mínimo de 4 palavras, `[...]` separa fragmentos em ordem a no máximo 1.500 caracteres; na TNU avisa **de quem é a frase** (TRANSCRIÇÃO, VOTO DIVERGENTE, ALEGAÇÃO DA PARTE, ENTRE ASPAS, NEGAÇÃO); lê primeiro o recibo local | só sem recibo |
| `diagnostico_ritmo_trf1` | estado do limitador, pausas e recusas, versão, modo do User-Agent, recibos em disco | não |

**Bases** (parâmetro `base`, mesmo portal): `trf1` (padrão — TRF1 + JEF1, painel avançado, voto não exposto);
`tnu` (tipos ACORDAO/DECISAOMONO/DECISAOPRES, filtro `tipo_acordao` = REPRESENTATIVO / RELEVANTE para os
precedentes qualificados, que saem com ★ e entram na citação; inteiro teor lido); `colegiado` (decisões
ADMINISTRATIVAS do Conselho — raramente serve a litígio). Nas bases `tnu`/`colegiado` não há painel avançado: o
filtro vai na sintaxe de campo da consulta (`nome[REL]`).

Fluxo: `buscar` → `obter_decisao` → `verificar_citacao` antes de qualquer aspas. A citação sai no padrão
`(TRF-1 - AC: nº, Relator: …, Data de Julgamento: …, TURMA, Data de Publicação: …) — verificação: <nível>`, com a
referência inteira em hiperlink para o inteiro teor **só quando o portal deu um link específico deste documento**
(`arquivo.trf1.jus.br`, eproc da TNU) — nunca no link genérico do PJe, que é o mesmo em todo resultado e apontaria
para o lugar errado.

## Como pesquisar bem

A busca casa palavras. Não use preposição, artigo nem pontuação na consulta (o motor não aceita): `"dano moral" E
negativação`, não `dano moral por negativação`. `grupos=[["dano moral"], ["negativação","inscrição indevida"]]`
vira `"dano moral" E (negativação OU "inscrição indevida")`. Radical com `$` (`desapropria$`), campo com
`[EMEN]`/`[DECI]`/`[REL]` (na `consulta`, ou no fim de um termo de `grupos`), ancoragem pela súmula/tema que os
julgados citam (a linha `Cita:` de cada resultado é a pista), e colheita de vocabulário do melhor resultado via
`obter_decisao_trf1`. O portal **recusa com erro** os caracteres `# ! + ' ; _ | - @` (disparado por
"auxílio-doença"): a ferramenta os troca por espaço; hífen dentro de palavra vira frase.

**Monte os grupos com o FATO julgado, nunca com a CONCLUSÃO que você espera.** Cada acórdão escreve a conclusão de
um jeito ("não equivale", "é inócua", "não afasta") e um grupo assim derruba a busca; medido no servidor irmão do
TJRO, a tese que zerava achou o acórdão em 5º lugar trocando o grupo da conclusão pelo do fato.

`por_pagina=50` sem filtro pode estourar o limite de saída de quem chama ("dano moral" sem data/órgão/relator deu
88 mil caracteres) — use 50 só junto de um filtro que já reduza o total.

## Limites, ditos sem rodeio

- **Base `trf1`: só ementa e dispositivo.** O voto não é exposto pelo portal; o inteiro teor dos casos antigos está
  em `arquivo.trf1.jus.br` atrás de Cloudflare, e o dos casos PJe é um link genérico à consulta pública. A ficha
  de precedente fica em `verificacao: "só ementa/índice"`; "inteiro teor lido" só depois de abrir o PDF no navegador.
  A conferência de citação cobre o que o portal expõe e **diz** que a atribuição (transcrição, voto vencido) não
  foi analisada — não é que não exista.
- **Base `tnu`: até 3 inteiros teores por chamada** de `obter_decisao_trf1` (orçamento do portal); o texto pode ser
  cortado pelo limite de saída, e aí a verificação diz "inteiro teor lido **EM PARTE**" — nunca promova a pleno.
  O recibo em disco guarda o texto inteiro.
- **Índice é indício, texto é prova.** Órgão, relator e data da citação vêm do fecho quando o inteiro teor foi
  lido (`orgao_fonte: fecho`); na base `trf1` a saída diz "índice do portal — não conferido no fecho".
- **Zero resultado não é "não existe no TRF1"** — é "o motor não casou essas palavras". `[PESQUISA NÃO
  REALIZADA]` é ainda outra coisa: o portal não foi perguntado.
- **Sob o mesmo número convivem vários documentos** (acórdão, embargos, monocrática): o `id` do documento é a
  chave, e a busca avisa quando o mesmo processo aparece com resultados opostos.
- Os alertas de atribuição são heurísticos: pegam o padrão comum, não tudo. Eles dizem "confira quem fala", não
  substituem ler o voto.
- Recibos ficam em `~/.trf1-jurisprudencia-recibos/` (0700/0600), um `<id>.json` por documento, com `texto`,
  `sha256`, `orgao_fonte` e os trechos que NÃO são palavra do órgão (`trechos_transcritos`, `trecho_divergente`),
  para que um verificador de citação avise em vez de aprovar. Recibo editado é posto de lado (`.inconsistente`).
  O inteiro teor da TNU nomeia partes: **não publique recibos.** Os fixtures deste repositório foram anonimizados.

## Ritmo e boa vizinhança

24 requisições por janela · 1,5 s entre elas · escada de janela que alarga a cada recusa real do portal
(1→5→10→20→30 min, só com 3+ requisições no último minuto) e relaxa após 100 sucessos · `403/429` seco → pausa de
10 min dobrando até 1 h, `Retry-After` vira piso · desafio de navegador na resposta (Cloudflare, "página
bloqueada"/"robotização") → pausa sem retentativa, nunca contornar · timeout de rede **não** arma o disjuntor ·
estado do disjuntor ilegível → pausa de 10 min (fail-closed). O estado fica em disco sob trava, compartilhado
entre processos da mesma máquina. `TRF1_USER_AGENT` troca o User-Agent, por conta e risco de quem troca; não rode
scripts soltos contra o portal fora do disjuntor. O servidor do CJF é pequeno e é de todos.

## Configuração avançada

`TRF1_MCP_DIR_RECIBOS` muda a pasta dos recibos (padrão `~/.trf1-jurisprudencia-recibos`).
`TRF1_MCP_SEM_AVISO_ATUALIZACAO=1` desliga a consulta única a `releases/latest` deste repositório, feita em thread
de fundo na subida do servidor para avisar de versão nova (nada da pesquisa sai daqui; só o GitHub vê o IP).

## Desenvolvimento

`--selftest` roda offline sobre respostas reais do portal em `fixtures/` (anonimizadas), com estado e recibos em
pasta temporária; `--selftest --online` faz 3 operações reais (~8 requisições). Protocolo medido do portal:
`references/protocolo-cjf.md`. Red teams: `references/red-team-2026-09-11.md` e `references/red-team-2026-09-22.md`.
As 8 determinações que motivaram a v1.1.0 (comparação com o servidor do TJSE): `references/correcoes-determinadas-2026-09-22.md`.
Histórico: `CHANGELOG.md`. Referência de terceiro consultada na concepção: `github.com/fxbarros/MCP-TRF1-Jurisprudencia`
(mesmo portal, sem licença — serviu de referência de protocolo, não de base de código; nada dele é redistribuído aqui).

## Apoie o projeto

O servidor é gratuito e de código aberto, e é mantido no tempo livre de um advogado: cada mudança do portal do CJF
exige diagnóstico, correção, testes e versão nova. Se ele economiza o seu tempo, você pode apoiar a continuidade do
trabalho com qualquer valor, por **Pix**:

> **Chave Pix (e-mail):** `robertogrecia@hotmail.com`

O apoio é voluntário e não muda nada no uso: o servidor continua igual para todos.

## Autor

**Roberto Grécia Bessa** — OAB/RO 7865-A
Instagram: [@robertogrecia](https://instagram.com/robertogrecia)

## Licença

MIT.
