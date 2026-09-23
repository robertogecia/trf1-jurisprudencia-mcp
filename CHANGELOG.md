# Histórico

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
