"""Teste ponta a ponta do tg_videos.py com cliente Telegram simulado (dados fictícios)."""
import asyncio, os, sys, tempfile, json, types as pytypes
from datetime import datetime, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ.update(TG_API_ID="1", TG_API_HASH="x", TG_CHANNEL="-100555", DATA_DIR=str(TMP / "data"),
                  DEST_DEFAULT="")
sys.path.insert(0, "/home/will/Projects/tg-video-sync")
import tg_videos as tg
from telethon import errors
from telethon.extensions import html as html_mode
from telethon.tl import types as T

_real_sleep = asyncio.sleep
async def fast_sleep(s, *a, **k): await _real_sleep(0)
asyncio.sleep = fast_sleep

D = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIZE = int(tg.CHUNK * 2.5)            # arquivo com 2,5 blocos
def content(mid): return bytes([mid % 256]) * SIZE

def doc(mid, video=True, name=None, size=SIZE):
    attrs = [T.DocumentAttributeFilename(name or f"aula{mid}.mp4")]
    if video:
        attrs.append(T.DocumentAttributeVideo(duration=120.0, w=1280, h=720, supports_streaming=True))
    return T.Document(id=mid, access_hash=1, file_reference=b"r", date=D,
                      mime_type="video/mp4" if video else "application/pdf",
                      size=size, dc_id=1, attributes=attrs)

def msg(mid, text="", media=None, grouped=None, entities=None):
    m = T.Message(id=mid, peer_id=T.PeerChannel(555), date=D, message=text, media=media,
                  grouped_id=grouped, entities=entities)
    m._client = FAKE
    return m

GUIDE_V1 = "🏆 GUIA\n\nDocuments\n#Doc001\n\n= 02. Docker e Containers\n#F0001 #F0002 #F0003 #F0004 #F0099"
GUIDE_V2 = GUIDE_V1 + "\n\n= 03. APIs\n#F0005"

class Fake:
    parse_mode = html_mode
    flood_sleep_threshold = 0
    def __init__(self):
        self.msgs = {}
        self.fail_once = {}           # mid -> nº de blocos antes de cair a conexão
        self.copy_refuse = set()      # ids cuja cópia o servidor recusa
        self.sent, self.edits, self.pins, self.download_offsets = [], [], [], []
        self.next_id = 1000
        self.dest_msgs = []
        self.msg_fail, self.edit_fail, self.fail_always = [], [], set()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def start(self): return self
    async def get_dialogs(self): return []
    async def get_me(self): return pytypes.SimpleNamespace(first_name="Teste", id=1)
    async def get_entity(self, ref):
        if ref == -100556: return pytypes.SimpleNamespace(id=556, title="Canal Ficticio 2", noforwards=False)
        if ref == -100555: return pytypes.SimpleNamespace(id=555, title="Canal Ficticio", noforwards=False)
        return pytypes.SimpleNamespace(id=777, title="Destino")
    async def iter_messages(self, chan, reverse=True, min_id=0):
        for mid in sorted(self.msgs):
            if mid > min_id: yield self.msgs[mid]
    async def get_messages(self, chan, ids=None, limit=None):
        if limit is not None: return self.dest_msgs[:limit]
        if isinstance(ids, list): return [self.msgs.get(i) for i in ids]
        return self.msgs.get(ids)
    async def iter_download(self, document, offset=0, request_size=None, file_size=None):
        self.download_offsets.append((document.id, offset))
        data = content(document.id)
        n = 0
        while offset < len(data):
            if document.id in self.fail_always: raise ConnectionError("falha permanente (simulado)")
            if self.fail_once.get(document.id) == n:
                del self.fail_once[document.id]
                raise ConnectionError("conexão caiu (simulado)")
            yield data[offset:offset + request_size]
            offset += request_size; n += 1
    async def send_file(self, target, media, **kw):
        if not isinstance(media, str) and media.id in self.copy_refuse:
            raise errors.ChatForwardsRestrictedError(request=None)
        self.next_id += 1
        self.sent.append(("file", "copy" if not isinstance(media, str) else "upload",
                          getattr(media, "id", Path(str(media)).name), kw.get("caption")))
        return pytypes.SimpleNamespace(id=self.next_id)
    async def send_message(self, target, text, **kw):
        if self.msg_fail: raise self.msg_fail.pop(0)
        self.next_id += 1
        self.sent.append(("text", None, None, text))
        return pytypes.SimpleNamespace(id=self.next_id)
    async def edit_message(self, target, mid, text, **kw):
        if self.edit_fail: raise self.edit_fail.pop(0)
        self.edits.append((mid, text))
    async def pin_message(self, target, m, notify=False): self.pins.append(m.id)

FAKE = Fake()
REAL_CLIENT = tg.client
tg.client = lambda: FAKE
def A(**kw):
    base = dict(channel=None, range=None, only_failed=False, batch=None, batch_mb=None,
                concurrency=None, all=True, pause=0, list=False, to=None, mode="auto",
                limit=0, allow_nonempty=False)
    base.update(kw); return pytypes.SimpleNamespace(**base)
import gc
def run(coro): gc.collect(); asyncio.run(coro)  # libera a trava de Store presa em traceback de SystemExit
def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: check.failed = True
check.failed = False

# ---------------------------------------------------------------- funções puras
cap = "#F0051 aula - By @fulano ❤️\n\n02. Docker e Containers\n=01. Docker na prática\n==60. Conectando no MongoDB através de nossa aplicação"
check(tg.title_from_caption(cap, "x") == "60. Conectando no MongoDB através de nossa aplicação", "título = última linha sem '='")
check(tg.find_tag(cap) == "F0051", "tag extraída")
check(tg.title_from_caption("#F0002 aula - By @fulano", "x") == "aula - By @fulano", "legenda de 1 linha sem a tag")
check(tg.title_from_caption("", "fallback") == "fallback", "sem legenda usa fallback")
check(tg.safe_name('a/b:c*?"<>|d') == "a b c d", "safe_name remove caracteres inválidos")
mp = tg.parse_guide(GUIDE_V2)
check(mp["F0003"] == "02. Docker e Containers" and mp["Doc001"] == "Documents" and mp["F0005"] == "03. APIs", "parse_guide mapeia tag -> módulo")
check(tg.take_batch([{"size": 10 * 2**20}] * 5, 10, 25) == [{"size": 10 * 2**20}] * 2, "take_batch respeita --batch-mb")
check(len(tg.take_batch([{"size": 99 * 2**20}], 10, 25)) == 1, "take_batch sempre leva >= 1 item")

# ---------------------------------------------------------------- hierarquia / índice (funções puras)
PH = tg.parse_hierarchy
check(PH(cap) == ["02. Docker e Containers", "01. Docker na prática", "60. Conectando no MongoDB através de nossa aplicação"],
      "parse_hierarchy: legenda completa com 3 níveis")
check(PH("02. Docker\n=01. Prática\n==49. Imagens base e filhas com ONBUILD.") == ["02. Docker", "01. Prática", "49. Imagens base e filhas com ONBUILD."],
      "parse_hierarchy: sem linha de hashtag, pontuação preservada")
check(PH("#F0001 aula\n\n==01. Intro") == [None, None, "01. Intro"], "parse_hierarchy: só a aula")
check(PH("#F0001 aula\n02. Docker") == ["02. Docker"], "parse_hierarchy: só o módulo")
check(PH("02. Docker\n==05. Aula") == ["02. Docker", None, "05. Aula"], "parse_hierarchy: módulo + aula sem submódulo")
check(PH("") == [] and PH(None) == [] and PH(123) == [] and PH("#F0001 #F0002") == [], "parse_hierarchy: vazio/None/só tags")
check(PH("M\n=S\n==A\n===Sub-aula") == ["M", "S", "A", "Sub-aula"], "parse_hierarchy: '===' vira nível 4")
check(PH("M\n=\n==   \n==A") == ["M", None, "A"], "parse_hierarchy: '=' sem texto é ignorado")
check(PH("  ==  02. Com espaços  ") == [None, None, "02. Com espaços"], "parse_hierarchy: espaços nas pontas")
check(PH("#docker\nM") == ["M"], "parse_hierarchy: hashtag genérica ignorada")
check(PH("Aula 49 - By @autor #devops\n=01. Sub\n==49. Aula") == [None, "01. Sub", "49. Aula"],
      "parse_hierarchy: hashtag no meio da linha de identificação também a descarta")
check(PH("02. Curso de C# e F#\n==07. Novidades do #Python3") == ["02. Curso de C# e F#", None, "07. Novidades do #Python3"],
      "parse_hierarchy: 'C#' não é hashtag; linha com '=' vale mesmo com '#'")
check(tg.md_escape("Mod #") == r"Mod \#" and tg.md_escape("Mod  ##") == r"Mod  \##" and tg.md_escape("C# x") == "C# x",
      "md_escape: '#' final (fechamento de cabeçalho ATX) é escapado")
_tmpd = TMP / "orfao"; _tmpd.mkdir()
(_tmpd / tg.INDEX_FILE).mkdir()                                     # força falha no os.replace
_st = pytypes.SimpleNamespace(m={"channel": "x"}, dir=_tmpd, ordered=lambda: [])
check(tg.safe_index(_st) is None and sorted(p.name for p in _tmpd.iterdir()) == [tg.INDEX_FILE],
      "falha ao gravar o índice não deixa .tmp órfão")
check(tg.md_escape("a*b_[c]`d") == r"a\*b\_\[c\]\`d" and tg.md_escape("#1 x") == r"\#1 x", "md_escape")

class FakeStore:
    def __init__(self, items, tmp):
        self.m, self.dir, self._items = {"channel": "Canal *Teste*", "channel_id": 1}, tmp, items
        tmp.mkdir(parents=True, exist_ok=True)
    def ordered(self): return sorted(self._items, key=lambda v: v["seq"])
def it_(seq, caption, status="pending", module=None, kind="video", tag="F%04d", drop_path=False):
    v = {"seq": seq, "kind": kind, "status": status, "module": module, "caption": caption,
         "tag": tag % seq if tag else None, "title": tg.title_from_caption(caption, f"arq{seq}"),
         "file": f"{seq:04d} - x.mp4", "path": PH(caption)}
    if drop_path: del v["path"]
    return v
fs = FakeStore([
    it_(1, "#F0001\nM1\n=S1\n==01. A", status="done"),
    it_(2, "#F0002\nM1\n=S1\n==02. B*negrito*"),
    it_(3, "#F0003\nM2\n==03. C"),
    it_(4, "#Doc004 material", kind="document", tag="Doc%03d"),        # sem nenhum nível e sem guia
    it_(5, "#F0005\n==05. D", module="Mód. do guia"),                  # sem nível 1: usa o guia
    it_(6, "#F0006\nM1\n=S1\n==06. E", status="done", drop_path=True),  # manifest antigo, sem 'path'
    it_(7, "#F0007\nM1", tag=None),                                    # só módulo: título vira a linha
    it_(8, "#Doc008 extra", module="Documents", kind="document", tag="Doc%03d"),
], TMP / "idx")
md = tg.write_index_md(fs).read_text()
L = md.splitlines()
check(L[0] == r"# Índice — Canal \*Teste\*" and "8 itens (6 vídeos, 2 documentos) · 2 baixados" in L[1], "índice: título e contadores")
check(L.count("## M1") == 3 and L.index("## M1") < L.index("## M2") < len(L) - 1 - L[::-1].index("## M1"),
      "índice: módulo que reaparece gera novo cabeçalho no ponto em que reaparece")
check("- [x] `F0001` 01. A — `0001 - x.mp4`" in L and r"- [ ] `F0002` 02. B\*negrito\* — `0002 - x.mp4`" in L,
      "índice: [x]/[ ], tag, título escapado e arquivo")
check(L[L.index("## M2") + 1] == "### (sem submódulo)", "índice: nível ausente vira (sem submódulo)")
check("## Sem classificação" in L and "- [ ] `Doc004` material — `0004 - x.mp4`" in L, "índice: sem nível vai para Sem classificação")
check(L[L.index("## Mód. do guia") + 1] == "### (sem submódulo)" and "- [ ] `F0005` 05. D — `0005 - x.mp4`" in L,
      "índice: sem nível 1 usa o módulo do guia")
check("- [x] `F0006` 06. E — `0006 - x.mp4`" in L, "índice: item antigo sem 'path' é calculado pela legenda")
i7 = L.index("- [ ] M1 — `0007 - x.mp4`")
check(L[i7 - 1] == "## M1", "índice: subir de nível repete o cabeçalho (não fica sob o submódulo anterior)")
check(L[L.index("## Documents") + 1] == "- [ ] `Doc008` extra — `0008 - x.mp4`", "índice: item sem nível com tag no guia")
check([int(l.rsplit("`", 2)[1][:4]) for l in L if l.startswith("- [")] == list(range(1, 9)), "índice: ordem por seq")
check(tg.md_code("a`b") == "`` a`b ``", "md_code com crase no nome")

# ---------------------------------------------------------------- scan
FAKE.msgs = {
    1: msg(1, "#F0001 aula\n\n==01. Intro", T.MessageMediaDocument(document=doc(1))),
    2: msg(2, "#F0002 aula\n==02. Instalação", T.MessageMediaDocument(document=doc(2)),
           entities=[T.MessageEntityBold(offset=18, length=10)]),
    3: msg(3, "", T.MessageMediaDocument(document=doc(3)), grouped=9),          # álbum sem legenda
    4: msg(4, "#F0003 aula\n==03. Álbum", T.MessageMediaDocument(document=doc(4)), grouped=9),
    5: msg(5, "#Doc001 material", T.MessageMediaDocument(document=doc(5, video=False, name="slides.pdf"))),
    6: msg(6, "aviso qualquer"),                                                   # texto comum: ignorado
    7: msg(7, "#F0004 aula\n==04. Fim", T.MessageMediaDocument(document=doc(7))),
    8: msg(8, GUIDE_V1),
}
run(tg.cmd_scan(A()))
st = tg.Store(555, readonly=True)
items = st.ordered()
check([v["id"] for v in items] == [1, 2, 3, 4, 5, 7], "scan: só vídeos+docs, em ordem de id")
check([v["seq"] for v in items] == [1, 2, 3, 4, 5, 6], "scan: seq contínuo")
check(items[2]["tag"] == "F0003" and items[2]["caption"] == items[3]["caption"], "álbum herda legenda")
check(items[4]["kind"] == "document" and items[4]["file"] == "0005 - Doc001 - material.pdf", "doc: nome numerado")
check(items[0]["file"] == "0001 - F0001 - 01. Intro.mp4", "vídeo: nome numerado com tag")
check("<strong>" in items[1]["caption_html"] or "<b>" in items[1]["caption_html"], "legenda HTML preserva negrito")
check(items[0]["module"] == "02. Docker e Containers", "módulo vindo do guia")
check("8" in st.m["guides"], "guia detectado")
# re-scan incremental: nada novo
run(tg.cmd_scan(A()))
check(len(tg.Store(555, readonly=True).ordered()) == 6, "re-scan incremental não duplica")

# ---------------------------------------------------------------- download com queda e retomada
FAKE.fail_once = {2: 1}         # item id 2 cai depois de 1 bloco
run(tg.cmd_download(A(range="1-3")))
st = tg.Store(555, readonly=True)
v2 = st.m["items"]["2"]
f2 = st.files / v2["file"]
check(v2["status"] == "done" and f2.read_bytes() == content(2), "download: arquivo íntegro após queda")
check((2, tg.CHUNK) in FAKE.download_offsets, "download: retomou do bloco 1 (não do zero)")
check(tg.caption_file(st, v2).read_text() == v2["caption"], "download: .txt com a legenda")
check(st.m["items"]["4"]["status"] == "pending", "--range limitou a seleção")
# .part parcial deixado de uma execução morta
part = st.files / (st.m["items"]["4"]["file"] + ".part")
part.write_bytes(content(4)[: tg.CHUNK + 100])   # bloco 1 completo + lixo parcial
FAKE.download_offsets.clear()
run(tg.cmd_download(A(range="4")))
st = tg.Store(555, readonly=True)
check((4, tg.CHUNK) in FAKE.download_offsets and (st.files / st.m["items"]["4"]["file"]).read_bytes() == content(4),
      "retomada de .part de execução anterior, alinhada ao bloco")

# ---------------------------------------------------------------- upload telegram
FAKE.copy_refuse = {doc(2).id}  # cópia do item 2 recusada -> fallback upload
FAKE.msgs[5] = msg(5, "#Doc001 material", T.MessageMediaDocument(document=doc(5, video=False, name="slides.pdf")))
FAKE.copy_refuse.add(5)         # doc 5 recusado e NÃO baixado -> para a ordem aqui
run(tg.cmd_upload(A(to="telegram:-100777")))
st = tg.Store(555, readonly=True)
key = "telegram:-100777"
ups = [st.m["items"][str(i)]["uploads"].get(key, {}).get("status") for i in (1, 2, 3, 4, 5, 7)]
check(ups == ["done", "done", "done", "done", None, None], f"upload para no buraco (item 5) mantendo ordem: {ups}")
check(st.m["items"]["2"]["uploads"][key]["via"] == "upload" and st.m["items"]["1"]["uploads"][key]["via"] == "copy",
      "auto: copy quando possível, upload quando recusado")
check(not any(s[0] == "text" for s in FAKE.sent), "guia não publicado antes de terminar")
# baixa o restante e reenvia
run(tg.cmd_download(A()))
run(tg.cmd_upload(A(to="telegram:-100777")))
st = tg.Store(555, readonly=True)
check(all(v["uploads"][key]["status"] == "done" for v in st.ordered()), "todos enviados")
order = [s[2] for s in FAKE.sent if s[0] == "file"]
check(order == [1, "0002 - F0002 - 02. Instalação.mp4", 3, 4, "0005 - Doc001 - material.pdf", 7], f"ordem de envio: {order}")
check(FAKE.sent[-1][0] == "text" and "GUIA" in FAKE.sent[-1][3] and FAKE.pins, "guia publicado ao final e fixado")
n_sent = len(FAKE.sent)
run(tg.cmd_upload(A(to="telegram:-100777")))
check(len(FAKE.sent) == n_sent, "re-upload idempotente (nada reenviado)")
# guia editado na origem -> edita no destino
FAKE.msgs[8] = msg(8, GUIDE_V2)
run(tg.cmd_scan(A()))
run(tg.cmd_upload(A(to="telegram:-100777")))
check(len(FAKE.edits) == 1 and "03. APIs" in FAKE.edits[0][1] and len(FAKE.sent) == n_sent, "guia editado no lugar, sem duplicar")

# destino não vazio protegido
FAKE.dest_msgs = [msg(1, "algo")]
try:
    run(tg.cmd_upload(A(to="telegram:-100888")))
    check(False, "destino não vazio bloqueado")
except SystemExit as e:
    check("já tem mensagens" in str(e), "destino não vazio bloqueado")
FAKE.dest_msgs = [T.MessageService(id=1, peer_id=T.PeerChannel(888), date=D, action=T.MessageActionChannelCreate(title="x"))]
FAKE.dest_msgs[0]._client = FAKE
n_sent = len(FAKE.sent)
run(tg.cmd_upload(A(to="telegram:-100888", limit=1)))
check(len(FAKE.sent) == n_sent + 1, "canal recém-criado (só msg de serviço) é aceito como vazio")

# ---------------------------------------------------------------- upload local
dest = TMP / "destino"
run(tg.cmd_upload(A(to=f"local:{dest}")))
names = sorted(p.name for p in dest.iterdir())
check(tg.GUIDE_FILE in names and "0001 - F0001 - 01. Intro.mp4" in names and "0001 - F0001 - 01. Intro.legenda.txt" in names,
      "local: vídeo + .txt + guia")
idx = (dest / tg.GUIDE_FILE).read_text()
alb = [x for x in FAKE.sent if x[2] in (3, 4)]
check(alb[0][3] == "" and "F0003" in alb[1][3], "álbum: legenda só no item original")
check("[02. Docker e Containers]" in idx and "F0001" in idx and "0005 - Doc001 - material.pdf" in idx, "índice tag -> arquivo")
st = tg.Store(555, readonly=True)
check(tg.INDEX_FILE in names and (dest / tg.INDEX_FILE).read_text() == (st.dir / tg.INDEX_FILE).read_text(),
      "local: INDICE.md copiado para o destino")
md = (st.dir / tg.INDEX_FILE).read_text()
check(st.m["items"]["1"]["path"] == [None, None, "01. Intro"], "scan grava 'path' no manifest")
check("## 02. Docker e Containers\n### (sem submódulo)\n- [x] `F0001` 01. Intro — `0001 - F0001 - 01. Intro.mp4`" in md
      and "## Documents\n- [x] `Doc001` material — `0005 - Doc001 - material.pdf`" in md
      and md.count("## 02. Docker e Containers") == 2, "índice real: módulo do guia, documento e cabeçalho repetido")
(st.dir / tg.INDEX_FILE).unlink()
run(tg.cmd_index(A()))
check((st.dir / tg.INDEX_FILE).exists(), "comando index regenera offline")

# robustez: falha na geração do índice não afeta scan, download nem upload em pasta
import io, contextlib
FAKE3 = Fake()
tg.client = lambda: FAKE3
FAKE3.msgs = {1: msg(1, "#F0001 aula\n02. M\n=01. S\n==01. A", T.MessageMediaDocument(document=doc(301))),
              2: msg(2, "", T.MessageMediaDocument(document=doc(302)))}  # sem legenda nenhuma
real_write_index = tg.write_index_md
def broken(st): raise RuntimeError("falha simulada no índice")
tg.write_index_md = broken
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    run(tg.cmd_scan(A(channel="-100557")))
    run(tg.cmd_download(A(channel="-100557")))
    run(tg.cmd_upload(A(channel="-100557", to=f"local:{TMP / 'destino3'}")))
st3 = tg.Store(777, readonly=True)
check(buf.getvalue().count("⚠ índice não gerado: RuntimeError: falha simulada") == 3
      and all(v["status"] == "done" and (st3.files / v["file"]).exists() for v in st3.ordered())
      and all(v["uploads"][f"local:{TMP / 'destino3'}"]["status"] == "done" for v in st3.ordered())
      and not (st3.dir / tg.INDEX_FILE).exists(), "índice com erro: só aviso; scan/download/upload concluem")
check(st3.m["items"]["2"]["path"] == [], "item sem legenda: path vazio")
tg.write_index_md = real_write_index
try:
    run(tg.cmd_index(A(channel="-100557")))
    check(tg.write_index_md is real_write_index and (st3.dir / tg.INDEX_FILE).exists(), "index após restaurar")
except SystemExit:
    check(False, "index após restaurar")
tg.client = lambda: FAKE


# ================================================================ cenário 2: achados da revisão
FAKE2 = Fake()
tg.client = lambda: FAKE2
os.environ["TG_CHANNEL"] = "-100556"
def photo(pid): return T.MessageMediaPhoto(photo=T.Photo(id=pid, access_hash=1, file_reference=b"r", date=D, sizes=[], dc_id=1))
def webpage(document=None): return T.MessageMediaWebPage(webpage=T.WebPage(id=1, url="https://ex.com", display_url="ex.com", hash=0, document=document))
LONG = "#F0010 aula\n==10. " + "Legenda longa " * 5
FAKE2.msgs = {
    1: msg(1, LONG, T.MessageMediaDocument(document=doc(101))),
    2: msg(2, "olha esse link https://t.me/c/1/2", webpage(doc(102))),          # preview com documento: NÃO é item
    3: msg(3, "#Doc002 notas", T.MessageMediaDocument(document=doc(103, video=False, name="notas.txt"))),
    4: msg(4, "#F0011 album com foto", photo(104), grouped=5),                     # portador da legenda é foto
    5: msg(5, "", T.MessageMediaDocument(document=doc(105)), grouped=5),
    6: msg(6, "", T.MessageMediaDocument(document=doc(106)), grouped=5),
    7: msg(7, GUIDE_V1.replace("Documents", "Documents https://ex.com"), webpage()),  # guia com preview de link
    8: msg(8, "#F0012 aula\n==12. Falha permanente", T.MessageMediaDocument(document=doc(108))),
}
run(tg.cmd_scan(A()))
st2 = tg.Store(556, readonly=True)
it = {v["id"]: v for v in st2.ordered()}
check(sorted(it) == [1, 3, 5, 6, 8], f"link preview com documento não vira item: {sorted(it)}")
check("7" in st2.m["guides"], "guia com link preview é detectado")
check(it[5]["caption_inherited"] is False and it[6]["caption_inherited"] is True and it[5]["tag"] == "F0011",
      "álbum com legenda em foto: 1º item enviado leva a legenda")

# lock: dois comandos no mesmo canal
lock_holder = tg.Store(556)
try:
    run(tg.cmd_download(A()))
    check(False, "trava impede dois comandos no mesmo canal")
except SystemExit as e:
    check("Outro comando" in str(e), "trava impede dois comandos no mesmo canal")
del lock_holder

# falha permanente + --all + lote 1: não pode entrar em loop infinito
FAKE2.fail_always = {108}
run(tg.cmd_download(A(batch=1)))
st2 = tg.Store(556, readonly=True)
it = {v["id"]: v for v in st2.ordered()}
check(it[8]["status"] == "failed" and all(it[i]["status"] == "done" for i in (1, 3, 5, 6)),
      "--all termina com falha permanente e baixa o resto")
FAKE2.fail_always = set()
# documento .txt da origem não é sobrescrito pela legenda
txt = st2.files / it[3]["file"]
check(txt.name.endswith("notas.txt") and txt.read_bytes() == content(103) and tg.caption_file(st2, it[3]).read_text() == "#Doc002 notas",
      "documento .txt preservado; legenda em .legenda.txt")
# mídia substituída na origem com .part existente: recomeça do zero
run(tg.cmd_download(A(only_failed=True)))
part8 = st2.files / (it[8]["file"] + ".part")
FAKE2.msgs[8] = msg(8, "#F0012 aula\n==12. Falha permanente", T.MessageMediaDocument(document=doc(208)))
st2 = tg.Store(556, readonly=True); v8 = st2.m["items"]["8"]
v8_status = v8["status"]
check(v8_status == "done", "only-failed baixa o que tinha falhado")
# força cenário: volta para pendente com .part parcial do doc antigo
m = json.loads((st2.dir / "manifest.json").read_text()); m["items"]["8"]["status"] = "pending"
(st2.dir / "manifest.json").write_text(json.dumps(m)); (st2.files / v8["file"]).unlink()
part8.write_bytes(content(108)[: tg.CHUNK])
FAKE2.download_offsets.clear()
run(tg.cmd_download(A(range="5")))
st2 = tg.Store(556, readonly=True)
check((208, 0) in FAKE2.download_offsets and (st2.files / st2.m["items"]["8"]["file"]).read_bytes() == content(208),
      "mídia trocada na origem: descarta .part antigo e baixa do zero")

# upload: legenda longa + falha no envio do texto não duplica a mídia
tg.CAPTION_LIMIT = 30
FAKE2.msg_fail = [ConnectionError("rede (simulado)")]
run(tg.cmd_upload(A(to="telegram:-100900", limit=1)))
files1 = [x for x in FAKE2.sent if x[0] == "file"]
texts1 = [x for x in FAKE2.sent if x[0] == "text"]
check(len(files1) == 1 and len(texts1) == 1 and files1[0][3] == "" and "Legenda longa" in texts1[0][3],
      f"legenda longa: mídia 1x + texto após retry (files={len(files1)}, texts={len(texts1)})")
# FloodWait longo no texto: encerra; na retomada envia só o texto
FAKE2.msg_fail = [errors.FloodWaitError(request=None, capture=5000)]
m = json.loads((st2.dir / "manifest.json").read_text()); m["items"]["1"]["uploads"] = {}
(st2.dir / "manifest.json").write_text(json.dumps(m, ensure_ascii=False))
FAKE2.sent.clear()
try:
    run(tg.cmd_upload(A(to="telegram:-100901", limit=1)))
    check(False, "FloodWait longo encerra")
except SystemExit as e:
    check("limite do Telegram" in str(e), "FloodWait longo encerra com orientação")
st2 = tg.Store(556, readonly=True)
check(st2.m["items"]["1"]["uploads"]["telegram:-100901"]["status"] == "media_sent", "estado media_sent persistido")
run(tg.cmd_upload(A(to="telegram:-100901", limit=1)))
check([x[0] for x in FAKE2.sent] == ["file", "text"], f"retomada envia só o texto: {[x[0] for x in FAKE2.sent]}")
tg.CAPTION_LIMIT = 1024

# erro determinístico do destino (sem permissão) não vira fallback nem retry
FAKE2.sent.clear()
orig_send_file = Fake.send_file
async def forbidden(self, target, media, **kw): raise errors.ChatWriteForbiddenError(request=None)
Fake.send_file = forbidden
try:
    run(tg.cmd_upload(A(to="telegram:-100902")))
    check(False, "erro de permissão encerra")
except SystemExit as e:
    check("admin" in str(e) and not FAKE2.sent, "erro de permissão encerra sem retry/fallback")
Fake.send_file = orig_send_file

# guia: FloodWait no edit não publica guia duplicado
run(tg.cmd_upload(A(to="telegram:-100903")))
n_guides = sum(1 for x in FAKE2.sent if x[0] == "text" and "GUIA" in (x[3] or ""))
FAKE2.msgs[7] = msg(7, GUIDE_V2, webpage())
run(tg.cmd_scan(A()))
FAKE2.edit_fail = [errors.FloodWaitError(request=None, capture=5000)]
try:
    run(tg.cmd_upload(A(to="telegram:-100903")))
except SystemExit:
    pass
n_guides2 = sum(1 for x in FAKE2.sent if x[0] == "text" and "GUIA" in (x[3] or ""))
check(n_guides == 1 and n_guides2 == 1, f"FloodWait no edit do guia não duplica ({n_guides}->{n_guides2})")
run(tg.cmd_upload(A(to="telegram:-100903")))
check(any("03. APIs" in e[1] for e in FAKE2.edits), "guia editado na retomada")

# rclone sem remote
try:
    run(tg.cmd_upload(A(to="rclone:pasta_sem_remote")))
    check(False, "rclone sem ':' rejeitado")
except SystemExit as e:
    check("remote" in str(e), "rclone sem ':' rejeitado")
# permissões
os.chmod(tg.DATA_DIR, 0o755); os.umask(0o022)
REAL_CLIENT()  # TelegramClient real (sem conectar): cria o .session
sess = tg.SESSION.with_suffix(".session")
check(oct(os.stat(tg.DATA_DIR).st_mode & 0o777) == "0o700" and oct(os.stat(sess).st_mode & 0o777) == "0o600",
      f"DATA_DIR 700 e .session 600 desde a criação ({oct(os.stat(sess).st_mode & 0o777)})")
tg.client = lambda: FAKE
os.environ["TG_CHANNEL"] = "-100555"


# status offline
run(tg.cmd_status(A(list=True)))
print("\nRESULTADO:", "FALHOU" if check.failed else "TUDO OK")
