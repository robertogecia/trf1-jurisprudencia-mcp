# Histórico

- **v1.4.1 (07/10/2026) — correção.** o servidor Python não abria no Python 3.10 e 3.11 (SyntaxError: barra invertida dentro da expressão de uma f-string, no texto do alerta de POSIÇÃO — só o Python 3.12+ aceita). Afetava as versões publicadas desde 06/10/2026; o CI do GitHub acusava. A extensão .mcpb (Node) não tinha o defeito.
- **v1.4.0 (07/10/2026) — negação em dois níveis e um número corrigido.** NEGAÇÃO em dois níveis. Continua "NEGAÇÃO:" quando a negação está colada ao trecho (até uma palavra antes) ou é existencial ("não há/houve/existe …", até cinco palavras). O resto que a regra anterior pegava sai como "NEGAÇÃO (distante)?", dizendo que em geral ela fecha a própria oração e não inverte o recorte. Gabarito cego e duplo em 120 trechos NOVOS de cinco tribunais (TJRO, TJSE, STJ, TCE-RO e TED-OAB; concordância 108/120, 12 adjudicados pela definição escrita), ponderado pela população: o alerta forte acerta 80% (falso alarme 6%); a regra anterior, sozinha, acertava 50% (falso alarme 33%) nesta amostra. Somados, os dois níveis avisam nos mesmos 72% das negações reais; o forte sozinho pega 53%. Correção do número publicado na v1.3.0: o ENTRE ASPAS da TNU tinha caído de 96% (ajuste) para 67% (validação) por defeito do GABARITO, não da ferramenta — em 4 dos 7 "falsos alarmes" o trecho está mesmo dentro de uma transcrição longa entre aspas cuja abertura ficou fora do recorte mostrado aos rotuladores (conferido no texto: aberturas 816 a 3.916 caracteres antes). Com essa adjudicação documental, a validação dá **86% de precisão** (cobertura ~68%); os 3 alarmes falsos que sobram são aspas soltas ou digitadas ao contrário. Paridade Python×Node: 12.802 conferências e 40.263 posições idênticas.
- **v1.3.0 (06/10/2026)** — primeira medição cega PRÓPRIA da TNU e POSIÇÃO NO JULGADO. 24 decisões novas da TNU com inteiro teor (34 no total), baixadas uma a uma com 20 s entre pedidos. `verificar_citacao_trf1` na base `tnu` diz onde a frase está (cabeçalho do documento, relatório, voto, ementa, acórdão, extrato de ata, voto de outro juiz): **99% às cegas** em 100 trechos, sem ajuste. O alerta VOTO DIVERGENTE, na TNU, passa a vir do documento: o voto de quem lavrou o acórdão ("assinado por FULANO, Relator do Acórdão", "nos termos do voto de FULANO, que lavrará o acórdão") é o condutor, inclusive quando é um voto-vista que venceu; os outros (vista, divergente, vogal, relator vencido) levam o alerta. Antes, qualquer trecho depois de um sinal de divergência levava o aviso. Na amostra de validação, os 19 disparos estavam todos em voto de quem não lavrou o acórdão (conferido no texto do acórdão de cada processo). Os demais alertas, medidos às cegas em 130 trechos de ajuste e 130 de validação (concordância por campo de 108 a 130 em 130): ENTRE ASPAS 96% e 67% de precisão; ALEGAÇÃO DA PARTE 44% e 80%; TRANSCRIÇÃO 66% e 62%; NEGAÇÃO 59% e 29%. NEGAÇÃO mais estreita: só avisa com a negação até 6 palavras antes do trecho; não avisa quando ela nega um particípio ("não utilizado pelo…") ou recusa uma alternativa ("…, e não sobre…"); "não é outro o entendimento", "não se desconhece" e "não se pode deixar de" afirmam. Gabarito cego e duplo: ajuste em 617 trechos já rotulados (TJSE, STJ, TRT14, OAB, TCE-RO), validação em 120 trechos NOVOS de cinco tribunais (concordância 114/120, 6 adjudicados): precisão 38% → 44%, falso alarme 42% → 31%, cobertura 100% → 98%. Continua o alerta mais fraco do bloco: é aviso para ler a frase, não veredito. OBITER DICTUM? reconhece também "registre-se, por oportuno", "a título de registro" e "apenas para registro" (6 de 6 obiter às cegas). Outras marcas testadas ficaram de fora por imprecisas: "de passagem" 67%, "por cautela" 25%, "ainda que se entenda/admita" 30%. Paridade Python×Node: 12.802 conferências e 40.263 posições idênticas.
- **v1.2.2 (06/10/2026)** — o recibo leva `trechos_obiter` (frases do inteiro teor sob marca de obiter) para o lint da
  `peticao-rg`; trecho com "nº" passa a ser localizado (antes, aspas/alegação/negação/obiter ficavam calados nele); "ainda que
  assim não fosse" não conta como negação. Paridade Python×Node: 5.062 conferências e os blocos de obiter idênticos.
- **v1.2.1 (06/10/2026)** — correção: o Python não tinha "sem razão" como operador de NEGAÇÃO e a extensão tinha (a regra entrou nos outros
  servidores antes do porte do TRF1, e o Python copiou o bloco antigo). Agora os dois avisam; caso novo no selftest e no teste do Node.
- **v1.2.0 (06/10/2026)** — regras de atribuição do TJRO v1.13/1.15 (aspas, alegação, negação) e o alerta OBITER DICTUM?.
  `verificar_citacao_trf1` passa a usar o mesmo bloco que o STJ, o TRT14 e o TJSE (`atribuicao13.js` no pacote Node é cópia
  byte a byte): ENTRE ASPAS por pareamento das aspas no documento inteiro (curvas e retas numa pilha só, reta orientada pelo
  vizinho, « »; tese fixada pelo próprio colegiado continua sem alerta), ALEGAÇÃO DA PARTE por verbo de relato com a parte
  como sujeito na FRASE do trecho, NEGAÇÃO por alcance (operador sem quebra de oração até o trecho, 3+ palavras dele; "não
  havendo dúvida" não nega; "sem razão" nega) e OBITER DICTUM? (marca contrafactual na mesma frase: "ainda que assim não
  fosse", "a título de argumentação"; só no inteiro teor, nunca em frase já entre aspas). Números cegos vêm do TJRO (aspas
  100%/70%, negação 68%, alegação 71-90%, obiter 86%), do STJ, do TRT14 e do TJSE; no TRF1 não há gabarito próprio — na base
  `trf1` (ementa e dispositivo, sem voto) só aspas e negação atuam; na TNU, tudo. Aspas simples retas (') deixam de contar
  como aspas (apóstrofo de "d'água"); as fixtures do selftest usavam-nas e passaram a aspas duplas. Python v1.2.0 e Node
  v1.2.0 (28 testes).
- **(23/09/2026)** — instalador de um clique: **[trf1-jurisprudencia-mcpb](https://github.com/robertogecia/trf1-jurisprudencia-mcpb)**,
  porte em Node.js empacotado como extensão `.mcpb` para o Claude Desktop (baixar, dois cliques, arrastar —
  sem Terminal), no molde do `tjro-jurisprudencia-mcp` e do `tcero-jurisprudencia-mcpb`. Fonte de verdade
  continua este repositório: mudança de comportamento entra aqui primeiro, com selftest e red team; o porte
  Node é testado por paridade (28 casos, os mesmos fixtures) e teste ao vivo contra o portal antes de publicar.
  README deste repositório aponta para o instalador logo no topo da seção "Instalar".

- **v1.1.0 (22/09/2026)** — porte das melhorias dos servidores irmãos (TJRO v1.7.x e TJSE v0.8.x), a partir das 8
  determinações de `references/correcoes-determinadas-2026-09-22.md` (comparação linha a linha com o servidor do
  TJSE, feita por agente Opus):
  - **`[PESQUISA NÃO REALIZADA — motivo]`** em toda falha de portal, ritmo, rede ou timeout — nunca mais "0
    resultados" quando o portal não foi perguntado; erro de parâmetro sai como "Erro de parâmetro". Exceção
    `PesquisaNaoRealizada`/`PortalRecusou` no lugar de `RuntimeError` genérico.
  - **Disjuntor fail-closed:** estado ilegível ou trava indisponível → pausa de 10 min, nunca "sem estado = sem
    limite". Desafio de navegador (Cloudflare, "página bloqueada") separado de bloqueio seco (403/429); a escada só
    alarga com 3+ requisições no último minuto (uma recusa isolada não é rajada); timeout de rede não arma o
    disjuntor; **recusa sistemática** (3 recusas sem sucesso entre elas) é dita no diagnóstico.
  - **User-Agent honesto** (`trf1-jurisprudencia-mcp/<versão> (… +https://github.com/robertogecia/trf1-jurisprudencia-mcp)`),
    testado ao vivo no CJF e no eproc da TNU antes de trocar; `TRF1_USER_AGENT` sobrescreve.
  - **Atribuição da frase** (portado do TJSE): `verificar_citacao_trf1` na base `tnu` avisa TRANSCRIÇÃO (bloco
    transcrito de outro tribunal, fechado por "(STJ, REsp …, Rel.)"), VOTO DIVERGENTE (após pedido de vênia /
    voto-vista), ENTRE ASPAS, ALEGAÇÃO DA PARTE (INSS/União/recorrente "sustenta") e NEGAÇÃO (negativa logo antes
    do trecho). Tese fixada pelo próprio colegiado entre aspas **não** dispara ENTRE ASPAS. Na base `trf1` a
    resposta diz que a atribuição não foi analisada porque o voto não é exposto.
  - **Piso e vão da conferência:** mínimo de 4 palavras e 25 caracteres; `[...]` só costura fragmentos a até
    1.500 caracteres um do outro; casamento por palavra inteira com pontuação opcional entre palavras (o «» do
    highlight e "art. 42" vs "art 42" não derrubam citação legítima).
  - **Fecho da TNU:** órgão, relator (inclusive "RELATOR DO ACÓRDÃO", em julgamento por maioria) e data lidos do
    inteiro teor; `orgao_fonte: fecho | índice`; a citação prefere o fecho e a resposta avisa DIVERGÊNCIA de
    órgão, relator ou data entre índice e texto. Base `trf1`: "órgão: índice do portal — não conferido no fecho".
  - **Recibos em disco** (`~/.trf1-jurisprudencia-recibos/<id>.json`, 0700/0600, escrita atômica, sha256): texto
    que o portal entregou, `orgao_fonte`, `trechos_transcritos` e `trecho_divergente` em bruto. `verificar_citacao`
    lê o recibo do processo antes de ir ao portal (0 requisições) e diz a fonte. Recibo editado → `.inconsistente`.
  - **"— verificação: só ementa/índice | inteiro teor lido | inteiro teor lido EM PARTE"** dentro da linha de
    citação (busca e decisão); EM PARTE quando o orçamento de saída cortou o texto.
  - **Sinais do julgado:** `Cita: Súmula X · Tema Y · IRDR Z · PUIL …` (âncora para a próxima busca), ⚠️ decisão
    monocrática/presidência, ⚠️ Turma Recursal (JEF).
  - Docstring de `grupos` com a regra "fato, não conclusão" medida no harness do TJRO.
  - **Crédito de autoria** uma vez por processo e **aviso de versão nova** (uma consulta a `releases/latest`, em
    thread de fundo, silenciosa sem rede; `TRF1_MCP_SEM_AVISO_ATUALIZACAO=1` desliga).
  - Lint da `peticao-rg` passa a conferir fichas do TRF1/TNU contra o recibo (id numérico do CJF ou `TNU` + 8
    dígitos; nº CNJ no `id_documento` é avisado; hosts `arquivo.trf1.jus.br`, `cjf.jus.br`, `pje2g.trf1.jus.br`).
  - Documentação no molde do TJSE: README com "Se não funcionar", `INSTALAR.md` para quem nunca usou Terminal,
    `requirements.txt`, botão de apoio.
  - Rejeitado por medição (TJSE, 0,1% dos processos): contar resultado por julgamento em vez de por documento.
  - Red team Opus offline: `references/red-team-2026-09-22.md`.

- **v1.0.0 (11–14/09/2026)** — servidor criado a partir do protocolo do portal do CJF (`references/protocolo-cjf.md`),
  com a disciplina do servidor do TJRO: disjuntor compartilhado entre processos, cache de 5 min, `grupos` de
  sinônimos, painel avançado lido ao vivo, bases `trf1`/`tnu`/`colegiado`, inteiro teor da TNU pelo eproc, citação
  pronta, id do documento como chave. Red team de 11/09 (12 achados corrigidos, `references/red-team-2026-09-11.md`);
  dois bugs do primeiro uso real (paginação em busca vazia; aviso duplicado por mutação do cache); hiperlink na
  citação só com link específico do documento (14/09).
