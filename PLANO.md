# PLANO — tg-video-sync (handoff para nova sessão)

> Documento de continuidade. Uma sessão nova deve ler **este arquivo + README.md** e seguir a seção **"Próximos passos"**.

## 1. Objetivo
1. **Camada 1 (download):** baixar **todos os vídeos e documentos** de um canal do Telegram ao qual o usuário tem acesso, em **lotes configuráveis**, com **status por item** (para retomar o que falhar).
2. **Camada 2 (upload):** reenviar **em ordem** para um destino (normalmente um **canal privado vazio e dedicado**), **replicando a legenda** de cada item, as **hashtags** (`#F0051`, `#Doc001`) e o **guia de navegação**. O guia é a mensagem final com hashtags agrupadas por módulo.

Exemplo de legenda na origem (fictício):
```
#F0051 aula - By @autor
02. Docker e Containers
=01. Docker na prática
==60. Conectando no MongoDB através de nossa aplicação
```

## 2. Decisões do usuário
| Tema | Decisão |
|---|---|
| Destino | Configurável (`--to` / `DEST_DEFAULT`); normalmente um canal privado vazio e dedicado |
| Estrutura em pasta | Pasta única numerada, sem subpastas por módulo |
| Escopo | Vídeos + documentos + guia. Fotos, áudios e textos soltos ficam de fora |
| Premium | A conta não é Premium, mas a opção fica configurável (`TG_PREMIUM`) |
| Tamanho do canal | Calculado dinamicamente no `scan` a partir dos metadados, sem download |
| Exclusão | Nunca apagar nada, nem local nem no destino |
| Links no guia | O guia não tem links, só hashtags |
| Modo de upload | `auto` como padrão: copia no servidor e, se recusado, faz upload do arquivo baixado |
| Pasta de dados | `~/Projects/tg-video-sync/data` |

## 3. Arquitetura (implementada)
- **Python 3.10 + Telethon 1.45** (MTProto, conta de usuário). Bot API foi descartada: limite de download de 20 MB e exige admin.
- **Script único:** `tg_videos.py` (cerca de 1.050 linhas).
- **Estado:** `data/<id_canal>/manifest.json`, com escrita atômica e trava `fcntl` exclusiva por canal. `status` é somente leitura.
- **Arquivos:** `data/<id_canal>/files/NNNN - <TAG> - <título>.<ext>`, mais `NNNN - ... .legenda.txt`.
- **Cache:** `data/channels.json` guarda a referência do canal e o ID, para `status` e upload em pasta funcionarem offline.
- **Sessão:** `data/tg_session.session`, com permissão 600 (umask 077) e `data/` com 700.

### Comandos
| Comando | Função |
|---|---|
| `login` | Autentica uma vez (telefone + código) |
| `chats` | Lista canais e grupos com seus IDs |
| `scan` | Inventário incremental: quantidade, GB, maior arquivo, guia, tags do guia sem arquivo, disco livre |
| `download` | `--batch N`, `--batch-mb M`, `--concurrency C`, `--all`, `--pause S`, `--range a-b`, `--only-failed` |
| `status` | Resumo, falhas e status por destino; `--list` mostra item a item |
| `index` | Regenera `data/<id_canal>/INDICE.md` (offline, somente leitura) |
| `upload` | `--to telegram:<id>`, `rclone:<remote:pasta>` ou `local:<pasta>`; `--mode auto/copy/upload`, `--limit`, `--pause`, `--allow-nonempty` |

### Regras de comportamento
- **Ordem:** `id` da mensagem em ordem crescente, numa sequência única com vídeos e documentos intercalados. O upload para o Telegram é **estritamente sequencial** e para no primeiro item que não pode ser enviado.
- **Retomada do download:** arquivo `.part`, offset alinhado a 512 KB, e a mensagem é buscada de novo a cada tentativa (o `file_reference` expira). Se o documento mudou na origem (`doc_id` diferente), o download recomeça do zero. O tamanho final é conferido.
- **Falhas:** até 5 por item, com espera crescente. FloodWait espera o tempo pedido e não conta como falha. `FROZEN_METHOD_INVALID` interrompe a execução. Itens que falharam ficam fora dos lotes seguintes da mesma execução, para não entrar em laço infinito.
- **Upload em duas etapas:** a mídia é enviada e o estado `media_sent` é gravado; só depois vai a legenda longa (acima de `TG_CAPTION_LIMIT`, contada em UTF-16) como texto. Uma nova tentativa nunca reenvia a mídia.
- **Fallback do modo `auto`:** só acontece para erros de cópia (`ChatForwardsRestricted`, `FileReference*`, `MediaEmpty`). Erros 400/403 do destino param na hora, com orientação.
- **Guia:** é detectado como mensagem de texto (ou com link preview) com 5 ou mais hashtags. É publicado e fixado **ao final**, quando todos os itens foram enviados. Se o original for editado, a mensagem publicada é **editada**; o guia só é republicado se tiver sido apagado no destino (`MessageIdInvalid`).
- **Álbum:** a legenda vai só no item que a tinha originalmente. Se o portador da legenda for uma foto (fora do escopo), o primeiro item enviado do álbum leva a legenda.
- **Destino em pasta:** pasta única, arquivo, `.legenda.txt` e `00 - GUIA DE NAVEGACAO.txt` (guia + índice tag → arquivo).
- **Origem com "Restringir salvamento de conteúdo":** a ferramenta recusa e encerra. Isso é intencional, por respeito ao dono do canal.

### Padrões por perfil (empíricos; o Telegram não publica limites fixos)
| | Concorrência | Lote | Pausa | Tamanho máximo de upload |
|---|---|---|---|---|
| Sem Premium | 2 | 10 | 45 s | 2000 MiB |
| Premium | 3 | 20 | 30 s | 4000 MiB |

## 4. Estado atual
- ✅ Implementação completa: `tg_videos.py`, `README.md`, `.env.example`, `.gitignore`, `requirements.txt`, `.venv/` com as dependências instaladas.
- ✅ Revisão adversarial feita (3 lentes + verificação). Os 16 problemas confirmados foram corrigidos.
- ✅ Índice em Markdown (`INDICE.md`, seção 6) implementado em 30/09/2026, com revisão adversarial (11 achados, 3 confirmados e corrigidos).
- ✅ `tests/test_tg.py`: teste ponta a ponta com cliente Telegram **simulado** e dados fictícios. **85/85 passam.**
  Para rodar: `.venv/bin/python tests/test_tg.py` (resultado esperado: `RESULTADO: TUDO OK`).
- ❌ **Nunca rodou contra o Telegram real** (depende do login do usuário).
- ⚠️ **O `.env` existe, mas está no formato errado.** Ele tem `api_id=`, `api_hash=`, `App title=` e `Nome curto=` (copiados do my.telegram.org), mas o script lê `TG_API_ID`, `TG_API_HASH`, `TG_CHANNEL` etc. Ainda faltam `TG_CHANNEL` e `DEST_DEFAULT`. **Não exibir nem logar os valores** (são credenciais).
- ❌ Não é um repositório git.

## 5. Próximos passos
0. ~~Implementar o índice em Markdown (seção 6)~~ ✅ feito. No uso real, conferir o `INDICE.md` gerado pelo `scan` (passo 4).
1. **Corrigir o `.env`** seguindo o `.env.example`: renomear `api_id` → `TG_API_ID` e `api_hash` → `TG_API_HASH`, e remover `App title`/`Nome curto` (não são usados). O usuário pode editar à mão; se for feito por script, nunca imprimir os valores.
2. `alias tgv='.venv/bin/python tg_videos.py'`, depois `tgv login`. É interativo, então o usuário roda no próprio terminal com `! tgv login`.
3. `tgv chats`, para preencher `TG_CHANNEL` (origem) e `DEST_DEFAULT=telegram:<id do canal privado vazio>` no `.env`.
4. `tgv scan` e validar: a contagem bate com o canal? O guia foi detectado? Há "tags do guia sem arquivo"?
5. `tgv download --range 1-3` e conferir arquivos, nomes e `.legenda.txt`. Interromper um download no meio (Ctrl+C) e rodar de novo para confirmar que ele retoma.
6. `tgv upload --limit 3` e conferir no canal de destino: ordem, legenda idêntica, hashtags clicáveis, via `copy` ou `upload` (`tgv status --list`).
7. Se estiver tudo certo: `tgv download --all` dentro do `tmux`, depois `tgv upload`. Ao final, o guia deve ser publicado e fixado.
8. (Opcional) `git init` + primeiro commit. O `.gitignore` já exclui `.env`, `*.session` e `data/`.

### Se algo falhar no uso real
- Rode `tgv status` para ver o último erro por item e por destino.
- Ajuste o código em `tg_videos.py`, acrescente um caso em `tests/test_tg.py` reproduzindo o problema com o fake e rode a suíte.
- Pontos que o teste simulado **não** cobre e merecem atenção no primeiro uso real:
  - comportamento de `get_entity` com ID `-100…` em sessão nova (já existe fallback via `get_dialogs`);
  - velocidade real e ocorrência de `FLOOD_PREMIUM_WAIT`;
  - se o `send_file` com o `Document` da origem (modo copy) preserva a miniatura;
  - se a busca por hashtag funciona no canal de destino.

## 6. Índice em Markdown pelos títulos (IMPLEMENTADO)

> Decisões tomadas na implementação, além da especificação abaixo:
> - `path` guarda `null` nos níveis ausentes (ex.: só a aula → `[null, null, "01. Intro"]`).
> - A hashtag só descarta linhas **sem** `=` (linha de identificação). Uma linha com `=` é título explícito e vale mesmo com `#` (ex.: `==07. Novidades do #Python3`). `C#` não conta como hashtag.
> - Nível 3 (`==`) é a linha do item. Com 4 ou mais níveis, o mais profundo é a linha e os demais viram cabeçalhos. Com só 1 ou 2 níveis, a linha usa o `title`.
> - Com um nível repetido na mesma legenda, vale o último. Limitação conhecida: uma linha solta sem `=` depois da aula (ex.: `By @autor` sem hashtag) substitui o módulo. Revisar se aparecer em legendas reais.
> - Quando o item sobe de nível (ex.: só módulo depois de módulo + submódulo), o cabeçalho do módulo é repetido para o item não ficar sob o submódulo anterior.

### Objetivo
Gerar um arquivo **separado** `INDICE.md` com a hierarquia de módulos e aulas, montada a partir da legenda de cada item. Exemplo de legenda (fictício):
```
#F0049 aula - By @autor
02. Docker e Containers
=01. Docker na prática
==49. Imagens base e filhas com ONBUILD.
```

### Regra de parsing (por item, a partir de `caption`)
| Linha da legenda | Nível | Exemplo |
|---|---|---|
| Sem `=` no início e sem hashtag | 1 (módulo) | `02. Docker e Containers` |
| Começa com `=` | 2 (submódulo) | `=01. Docker na prática` |
| Começa com `==` | 3 (aula) | `==49. Imagens base e filhas com ONBUILD.` |
| Contém hashtag (`#F0049 aula - By ...`) | ignorada | linha de identificação, não é título |

- Nível = quantidade de `=` no início + 1. Se aparecer `===` ou mais, aceitar níveis mais profundos sem quebrar.
- Remover os `=` e os espaços das pontas. Manter o texto como está, com numeração e pontuação.
- Guardar no item do manifest: `"path": ["02. Docker e Containers", "01. Docker na prática", "49. Imagens base ..."]`. Se não houver hierarquia, guardar `"path": []`.

### Informação incompleta: tolerância obrigatória
- A legenda pode não ter todas as informações. **Nada disso pode afetar o download nem o upload.**
- Sem nenhum nível: o item vai para a seção `## Sem classificação`, pelo título atual (`title`) ou pelo nome do arquivo.
- Só alguns níveis (por exemplo, só a aula, ou módulo + aula sem submódulo): o item é posicionado com os níveis que existem. Os ausentes viram `(sem submódulo)` ou `(sem módulo)`. Não herdar nível de outro item, para não classificar errado.
- Álbum com legenda herdada: usa o `path` herdado normalmente.
- **Fallback do módulo pelo guia:** se o item não tiver nível 1, mas a tag dele estiver no guia (`v["module"]`, já calculado hoje), usar esse módulo.
- Toda a geração roda em `try/except`. Se falhar, só aparece um aviso (`⚠ índice não gerado: <erro>`) e o comando continua e termina normalmente.

### Formato do `INDICE.md`
```markdown
# Índice — <nome do canal>
_Gerado em <data> · <N> itens (<V> vídeos, <D> documentos) · <B> baixados_

## 02. Docker e Containers
### 01. Docker na prática
- [ ] `F0049` 49. Imagens base e filhas com ONBUILD. — `0049 - F0049 - 49. Imagens base e filhas com ONBUILD.mp4`
- [x] `F0050` 50. ... — `0050 - ....mp4`

## Sem classificação
- [ ] `Doc001` material — `0254 - Doc001 - material.pdf`
```
- A ordem é sempre por `seq` (ordem do canal). Os cabeçalhos aparecem na primeira ocorrência, e um módulo que reaparece mais adiante gera um novo cabeçalho no ponto em que reaparece, para não reordenar.
- `[x]` indica item baixado (`status == "done"`) e `[ ]` indica pendente. Isso faz o arquivo funcionar também como checklist de progresso.
- Escapar caracteres que quebram Markdown nos títulos (`*`, `_`, `[`, `]`, `` ` ``, `#` no início).
- Documentos aparecem com o mesmo formato.

### Onde e quando gerar
- Arquivo: `data/<id_canal>/INDICE.md`.
- Gerado automaticamente ao final de `scan`, de `download` e do upload em pasta (best-effort).
- Novo comando `tgv index`, que regenera sob demanda e funciona offline (usa `offline_store`, em modo somente leitura).
- Upload `local:` e `rclone:`: copiar o `INDICE.md` junto, como já é feito com `00 - GUIA DE NAVEGACAO.txt`.
- Telegram: **não** publicar por padrão (fora do escopo pedido). Opcional futuro: `--with-index` enviaria o arquivo como documento ao final, depois do guia.

### Implementação (passos)
1. `parse_hierarchy(caption) -> list[str]`: função pura que nunca lança exceção (entrada `None` ou vazia retorna `[]`).
2. Em `new_item`, gravar `path`. Para itens antigos do manifest sem `path`, calcular na hora a partir de `caption`; não é preciso rodar `scan` de novo.
3. `write_index_md(st) -> Path`: monta o Markdown, faz a escrita atômica (tmp + `os.replace`) e retorna o caminho.
4. `safe_index(st)`: chama `write_index_md` em `try/except Exception` e imprime um aviso se falhar.
5. Chamar `safe_index` no fim de `cmd_scan`, de `cmd_download` e de `upload_files` (e copiar o arquivo para o destino em `upload_files`).
6. Subcomando `index` no argparse.
7. README: documentar o `INDICE.md` e o comando `index`.

### Testes a acrescentar em `tests/test_tg.py`
- `parse_hierarchy` com a legenda completa de 3 níveis, sem linha de hashtag, só com a aula, só com o módulo, vazia/`None`, com `===` (nível 4), e com `=` sem texto.
- Item sem nenhuma hierarquia vai para "Sem classificação" e usa o módulo do guia quando a tag estiver nele.
- Ordem preservada por `seq`, com cabeçalho repetido quando o módulo reaparece.
- `[x]`/`[ ]` conforme o status.
- **Robustez:** forçar uma exceção dentro de `write_index_md` (monkeypatch) e verificar que `scan` e `download` terminam normalmente e os arquivos são baixados.
- Destino `local:` recebe o `INDICE.md`.

## 7. Melhorias opcionais (não pedidas; só se o usuário quiser)
- Uploads mais rápidos (conexões paralelas / FastTelethon) ou a ferramenta pronta `tdl`. Confirmar a documentação antes.
- `ffmpeg` para gerar miniatura no modo upload (`sudo apt install ffmpeg`); o código já usa se existir.
- Reescrever links `t.me/...` do guia para o destino (hoje não é necessário: o guia não tem links).

## 8. Segurança e uso
- O `.session` equivale à senha da conta: nunca compartilhar nem versionar. Se vazar, encerrar a sessão em *Configurações → Dispositivos*.
- Credenciais só no `.env`; exemplos e testes usam apenas dados fictícios.
- Uso pessoal: o canal de destino deve permanecer privado; não redistribuir o conteúdo.
