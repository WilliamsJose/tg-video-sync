# tg-video-sync

Baixa os **vídeos e documentos** de um canal do Telegram ao qual você tem acesso, em lotes, com **status por item** e **retomada**. Depois reenvia tudo **em ordem** para um destino (normalmente um canal privado), preservando a **legenda**, as **hashtags** (`#F0051`) e o **guia de navegação**.

> Use apenas para conteúdo que você tem direito de baixar, como cópia pessoal. Mantenha o canal de destino privado. Se o dono do canal de origem ativou *"Restringir salvamento de conteúdo"*, a ferramenta respeita essa restrição e não baixa nem copia nada.

## 1. Instalação

```bash
cd ~/Projects/tg-video-sync
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # depois edite o .env
```

Credenciais: em <https://my.telegram.org> → *API development tools*, crie um app e copie `api_id` e `api_hash` para o `.env`.

## 2. Primeiro uso

```bash
alias tgv='.venv/bin/python tg_videos.py'

tgv login                  # pede telefone e código (uma vez só)
tgv chats                  # lista canais/IDs -> coloque TG_CHANNEL e DEST_DEFAULT no .env
tgv scan                   # inventário: quantidade, GB, maior arquivo, guia, disco livre
tgv index                  # regenera o INDICE.md (offline; também é gerado por scan/download)
```

## 3. Download

```bash
tgv download --range 1-3          # teste com 3 itens
tgv download --all                # todos, em lotes, com pausa entre lotes
tgv download --batch 5 --batch-mb 3000 --concurrency 2
tgv download --only-failed        # tenta de novo só os que falharam
tgv status                        # resumo + falhas
tgv status --list                 # item a item (status do download e de cada destino)
```

- **Padrões sem Premium:** concorrência 2, lote 10, pausa de 45 s. Com `TG_PREMIUM=true`: 3, 20 e 30 s. São valores empíricos, porque o Telegram não publica limites fixos. Concorrência acima de 4 costuma gerar mais esperas forçadas (*FloodWait*) do que ganho.
- **Retomada:** pode interromper (Ctrl+C, queda de rede, hibernação do Windows) e rodar o mesmo comando de novo. O download continua do último bloco de 512 KB.
- **Status por item:** `pending → downloading → done | failed`, com número de tentativas e último erro no `manifest.json`.
- **Arquivos:** ficam em `data/<id_canal>/files/`, com nome como `0051 - F0051 - 60. Conectando no MongoDB através de nossa aplicação.mp4`, mais um `.legenda.txt` com a legenda completa (sufixo próprio, para nunca sobrescrever um documento `.txt` da origem).
- Para execuções longas, use `tmux` para não perder o processo se o terminal fechar.
- Um comando por vez em cada canal: se você rodar `download` e `upload` ao mesmo tempo, o segundo é recusado, porque os dois gravariam o mesmo manifest. `status` é só leitura e pode rodar a qualquer momento.
- Itens que falham 5 vezes ficam `failed` e são pulados até o fim da execução. Tente de novo depois com `download --only-failed`.

## 4. Upload

```bash
tgv upload --limit 3                        # teste: 3 itens para DEST_DEFAULT
tgv upload                                  # todos, na ordem
tgv upload --to telegram:-1009876543210     # outro destino
tgv upload --to local:/home/usuario/Videos/Curso
tgv upload --to rclone:gdrive:Cursos/Curso  # exige rclone instalado e configurado
```

### Destino Telegram

- **Pré-requisitos:** canal **vazio e dedicado**, e você precisa ser admin com permissão de postar. Se o canal já tiver mensagens, o envio é bloqueado; use `--allow-nonempty` para enviar mesmo assim.
- **`--mode auto`** (padrão): primeiro tenta copiar a mídia no próprio servidor do Telegram, que é instantâneo e não precisa de download. Se a cópia for recusada, envia o arquivo baixado. Também existem `--mode copy` e `--mode upload`.
- **Ordem estrita:** o envio para no primeiro item que ainda não pode ser enviado e mostra o comando `download --range N` para resolver.
- **Legenda:** vai idêntica, em HTML, com as hashtags clicáveis. Se passar de `TG_CAPTION_LIMIT`, vai como mensagem de texto logo após o arquivo.
- **Guia:** é publicado e fixado **depois que todos os itens foram enviados**. Se o guia original for editado, rode `scan` e `upload` de novo: o guia publicado é editado, sem duplicar.
- **Álbuns:** cada item vai como mensagem separada. A legenda vai só no item que a tinha originalmente.
- **Miniatura:** se o `ffmpeg` estiver instalado (`sudo apt install ffmpeg`), ela é gerada nos vídeos enviados em modo upload. No modo copy, a miniatura original é mantida.

### Destino em pasta (local ou rclone)

Pasta única numerada, com um `.legenda.txt` por arquivo, o `00 - GUIA DE NAVEGACAO.txt` (o texto do guia mais um índice tag → arquivo) e o `INDICE.md` (seção 5).

## 5. Índice em Markdown (`INDICE.md`)

`data/<id_canal>/INDICE.md` mostra módulos, submódulos e aulas montados a partir da legenda de cada item, na ordem do canal. Também serve de checklist: `[x]` indica item baixado e `[ ]`, pendente.

```markdown
## 02. Docker e Containers
### 01. Docker na prática
- [x] `F0049` 49. Imagens base e filhas com ONBUILD. — `0049 - F0049 - 49. Imagens base e filhas com ONBUILD.mp4`
```

- **Como a legenda é lida:** linha sem `=` é módulo, `=` é submódulo, `==` é aula e `===` ou mais são níveis mais profundos. Linhas sem `=` que contêm hashtag (`#F0049 aula - By ...`) são a identificação do item e ficam de fora. Uma linha com `=` vale mesmo que tenha `#`. O texto fica como está, com numeração e pontuação.
- **Legenda incompleta:** o item entra com os níveis que tem, e os que faltam aparecem como `(sem módulo)` ou `(sem submódulo)`. Um nível nunca é herdado de outro item. Se faltar o módulo e a tag estiver no guia, vale o módulo do guia. Sem nenhum nível, o item vai para `## Sem classificação`.
- **Ordem:** sempre a do canal. Se um módulo reaparece mais adiante, ganha um novo cabeçalho no ponto em que reaparece.
- **Quando é gerado:** ao final de `scan`, de `download` e do upload em pasta (que também copia o arquivo para o destino), ou sob demanda com `tgv index`, que funciona offline. No destino Telegram ele não é publicado.
- Uma falha ao gerar o índice só mostra um aviso (`⚠ índice não gerado: ...`). Download e upload continuam normalmente.

## 6. Segurança

- `data/tg_session.session` **dá acesso total à sua conta do Telegram**. Ele fica com permissão 600, entra no `.gitignore` e nunca deve ser compartilhado. Se vazar, encerre a sessão em *Configurações → Dispositivos*.
- Credenciais só no `.env`, que não é versionado.
- Use sua conta pessoal. Mantenha o projeto fora de repositórios de trabalho.

## 7. Limitações conhecidas

- O Telegram limita a velocidade de download de contas sem Premium; aumentar a concorrência não contorna isso.
- Se o processo cair **entre** o envio de uma mídia e a gravação do manifest, ela pode ser reenviada em duplicidade na próxima execução. A janela é de milissegundos. O estado é gravado logo após cada envio; com legenda longa, a mídia e o texto são etapas separadas, então uma falha no texto nunca reenvia a mídia.
- Erros do destino que não se resolvem tentando de novo (sem permissão, legenda longa demais) param o envio na hora, com orientação, sem novas tentativas.
- O destino é identificado pelo texto exato do `--to`. Use sempre a mesma forma (por exemplo, sempre o ID numérico) para a retomada reconhecer o que já foi enviado.

> Criado em 1 prompt com Claude Code
