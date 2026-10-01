#!/usr/bin/env python3
"""tg-video-sync — baixa vídeos e documentos de um canal do Telegram (em lotes, com
retomada e status por item) e reenvia em ordem para um destino, preservando legendas,
hashtags e o guia de navegação.

Comandos:
  login     autentica a conta (uma vez)
  chats     lista canais/grupos e seus IDs
  scan      inventário incremental do canal (quantidade, tamanho, guia)
  download  baixa em lotes (--batch / --batch-mb / --concurrency / --all)
  status    resumo, falhas e (--list) item a item
  upload    reenvia em ordem: telegram:<chat> | rclone:<remote:pasta> | local:<pasta>
  index     regenera o INDICE.md (módulos e aulas pela legenda; funciona offline)
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.tl.types import (DocumentAttributeFilename, DocumentAttributeVideo,
                               MessageMediaDocument, MessageMediaWebPage)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def _data_dir():
    p = Path(os.getenv("DATA_DIR") or "data").expanduser()
    return p if p.is_absolute() else HERE / p


DATA_DIR = _data_dir()
SESSION = DATA_DIR / "tg_session"
CHANNELS = DATA_DIR / "channels.json"   # cache TG_CHANNEL -> id (permite status/upload em pasta offline)

PREMIUM = os.getenv("TG_PREMIUM", "false").strip().lower() in ("1", "true", "yes", "sim")
# Valores empíricos (o Telegram não publica limites fixos); todos ajustáveis por flag.
# max_upload: 4000/8000 partes de 512 KB por arquivo (conta comum/Premium).
PROFILE = (dict(concurrency=3, batch=20, pause=30, max_upload=4000 * 2**20) if PREMIUM
           else dict(concurrency=2, batch=10, pause=45, max_upload=2000 * 2**20))
CAPTION_LIMIT = int(os.getenv("TG_CAPTION_LIMIT") or 1024)

CHUNK = 512 * 1024          # tamanho de requisição do MTProto; offsets de retomada alinhados a ele
MAX_ATTEMPTS = 5            # falhas (não FloodWait) toleradas por item numa execução
FLOOD_SLEEP = 120           # FloodWait até esse valor (s) o Telethon dorme sozinho
LONG_FLOOD = 900            # no upload, FloodWait maior que isso encerra (retome depois)
TAG_RE = re.compile(r"#([A-Za-z]+\d+)\b")   # ex.: #F0051, #Doc001
GUIDE_MIN_TAGS = 5          # mensagem só de texto com >= N tags = guia de navegação
GUIDE_FILE = "00 - GUIA DE NAVEGACAO.txt"
INDEX_FILE = "INDICE.md"
HASHTAG_RE = re.compile(r"(?<![\w#])#\w*[^\W\d_]")   # qualquer hashtag (#docker), sem pegar 'C#'
LEAF_LEVEL = 3              # na legenda, '==' (nível 3) é a aula; níveis acima viram cabeçalhos


COPY_ERRORS = (errors.ChatForwardsRestrictedError, errors.FileReferenceExpiredError,
               errors.FileReferenceInvalidError, errors.MediaEmptyError)


class NotReady(Exception):
    """Item não pode ser enviado ainda (não baixado e cópia indisponível)."""


# ------------------------------------------------------------------ helpers

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_size(n):
    n = n or 0
    s = f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.1f} MB"
    return s.replace(".", ",")


def fmt_eta(seconds):
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def env(name):
    val = os.getenv(name)
    if not val:
        sys.exit(f"Defina {name} no arquivo .env (veja .env.example)")
    return val


def parse_chat(s):
    s = str(s).strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def find_tag(caption):
    m = TAG_RE.search(caption or "")
    return m.group(1) if m else None


def title_from_caption(caption, fallback):
    """Última linha não vazia da legenda, sem tags e sem marcadores de hierarquia (=, -)."""
    for line in reversed((caption or "").splitlines()):
        title = TAG_RE.sub("", line).strip().lstrip("=-*> ").strip()
        if title:
            return title
    return fallback


def parse_hierarchy(caption):
    """Níveis da legenda pela quantidade de '=' no início: sem '=' -> 1 (módulo), '=' -> 2
    (submódulo), '==' -> 3 (aula), '===' -> 4... Linha sem '=' que contém hashtag é a de
    identificação e fica de fora; com '=' é título explícito e vale mesmo com '#'. Retorna a lista
    por nível, com None nos níveis ausentes. Nunca lança exceção."""
    if not isinstance(caption, str):
        return []
    try:
        path = []
        for line in caption.splitlines():
            s = line.strip()
            level = len(s) - len(s.lstrip("="))
            if not s or (level == 0 and (TAG_RE.search(s) or HASHTAG_RE.search(s))):
                continue
            text = s.lstrip("=").strip()
            if not text:
                continue
            path += [None] * (level + 1 - len(path))
            path[level] = text  # nível repetido: vale o último
        return path
    except Exception:
        return []


def tg_len(s):
    """Tamanho como o Telegram conta (unidades UTF-16; emoji conta 2)."""
    return len((s or "").encode("utf-16-le")) // 2


def caption_file(st, v):
    # sufixo próprio: nunca colide com um documento .txt da origem
    return st.files / (v["base"] + ".legenda.txt")


def media_doc(msg):
    """Documento anexado de fato (ignora o documento de um link preview)."""
    return msg.media.document if msg and isinstance(msg.media, MessageMediaDocument) else None


def safe_name(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen].rstrip(" .") or "arquivo"


def is_guide(text):
    return len(TAG_RE.findall(text or "")) >= GUIDE_MIN_TAGS


def parse_guide(text):
    """Cada linha sem tags vira o 'módulo' corrente; as tags seguintes pertencem a ele.
    Ex.: '= 02. Docker e Containers' + '#F0001 #F0002 ...' -> {'F0001': '02. Docker e Containers'}"""
    module, out = None, {}
    for line in (text or "").splitlines():
        s = line.strip()
        tags = TAG_RE.findall(s)
        if tags:
            for t in tags:
                out.setdefault(t, module)
        elif s:
            module = s.lstrip("=-*> ").strip()
    return out


def classify(msg):
    """'video' | 'document' | None (fora do escopo)."""
    if not media_doc(msg) or msg.sticker or msg.gif or msg.voice or msg.audio or msg.video_note:
        return None
    return "video" if msg.video else "document"


def skipped_label(msg):
    if msg.photo:
        return "fotos"
    if msg.sticker:
        return "stickers"
    if msg.gif:
        return "gifs"
    if msg.voice or msg.audio:
        return "áudios"
    if msg.video_note:
        return "vídeos redondos"
    return "outros"


# --------------------------------------------------------------- manifest

class Store:
    """Manifest de um canal de origem: DATA_DIR/<id>/manifest.json + files/."""

    def __init__(self, chan_id, readonly=False):
        self.dir = DATA_DIR / str(chan_id)
        self.files = self.dir / "files"
        self.path = self.dir / "manifest.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        if not readonly:
            self._lock = open(self.dir / ".lock", "w")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit("Outro comando já está usando este canal (download/upload/scan). "
                         "Rode um de cada vez — ou use 'status', que é só leitura.")
        self.m = {"channel": None, "channel_id": chan_id, "last_id": 0, "items": {}, "guides": {}}
        if self.path.exists():
            self.m.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.m, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # escrita atômica: o manifest nunca fica pela metade

    def ordered(self):
        return sorted(self.m["items"].values(), key=lambda v: v["seq"])

    def guides(self):
        return sorted(self.m["guides"].values(), key=lambda g: g["id"])

    def module_map(self):
        mp = {}
        for g in self.guides():
            if not g.get("deleted"):
                for t, mod in parse_guide(g["text"]).items():
                    mp.setdefault(t, mod)
        return mp


def load_cache():
    return json.loads(CHANNELS.read_text(encoding="utf-8")) if CHANNELS.exists() else {}


def channel_ref(args):
    return getattr(args, "channel", None) or env("TG_CHANNEL")


def offline_store(ref, readonly=False):
    chan_id = load_cache().get(str(ref))
    if not chan_id:
        sys.exit(f"Canal '{ref}' ainda não indexado. Rode 'scan' primeiro.")
    return Store(chan_id, readonly)


# ----------------------------------------------------------------- client

def client():
    os.umask(0o077)  # sessão, manifest e arquivos nascem privados (só o seu usuário)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    c = TelegramClient(str(SESSION), int(env("TG_API_ID")), env("TG_API_HASH"),
                       receive_updates=False)
    c.flood_sleep_threshold = FLOOD_SLEEP
    c.parse_mode = "html"  # msg.text vira HTML -> formatação da legenda é preservada no reenvio
    return c


def secure_session_file():
    p = SESSION.with_suffix(".session")
    if p.exists():
        os.chmod(p, 0o600)


async def get_entity(c, ref):
    try:
        return await c.get_entity(parse_chat(ref))
    except ValueError:
        await c.get_dialogs()  # sessão nova não conhece IDs numéricos: carrega os diálogos e tenta de novo
        return await c.get_entity(parse_chat(ref))


async def resolve_channel(c, ref):
    chan = await get_entity(c, ref)
    # if getattr(chan, "noforwards", False):
    #     sys.exit("O canal de origem tem 'Restringir salvamento de conteúdo' ativado pelo dono. "
    #              "A ferramenta respeita essa restrição e não baixa nem copia o conteúdo.")
    cache = load_cache()
    cache[str(ref)] = chan.id
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHANNELS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    os.replace(tmp, CHANNELS)
    secure_session_file()
    return chan


# --------------------------------------------------------------- commands

async def cmd_login(args):
    async with client() as c:
        me = await c.get_me()
        print(f"Autenticado como {me.first_name} (id {me.id}). Sessão em {SESSION}.session")
    secure_session_file()


async def cmd_chats(args):
    async with client() as c:
        async for d in c.iter_dialogs():
            if d.is_channel or d.is_group:
                print(f"{d.id:>16}  {d.name}")
    secure_session_file()


def new_item(msg, kind, seq, caption, caption_html, inherited=False):
    f = msg.file
    orig = f.name or f"{kind}_{msg.id}{f.ext or ''}"
    tag = find_tag(caption)
    title = title_from_caption(caption, Path(orig).stem)
    base = f"{seq:04d} - " + (f"{tag} - " if tag else "") + safe_name(title)
    ext = Path(orig).suffix or f.ext or ""
    return {
        "id": msg.id, "seq": seq, "kind": kind, "date": msg.date.isoformat(),
        "tag": tag, "module": None, "title": title, "path": parse_hierarchy(caption),
        "caption": caption or "", "caption_html": caption_html or "",
        "caption_inherited": inherited,  # álbum: legenda herdada só para nomear; não reenvia
        "doc_id": media_doc(msg).id, "orig_name": orig, "mime": f.mime_type, "base": base, "file": base + ext,
        "size": f.size or 0, "duration": f.duration, "width": f.width, "height": f.height,
        "status": "pending", "attempts": 0, "error": None, "updated": now(),
        "uploads": {},
    }


def guide_entry(msg, old=None):
    html = msg.text or ""
    return {"id": msg.id, "text": msg.message or "", "text_html": html,
            "hash": hashlib.sha1(html.encode()).hexdigest(), "deleted": False,
            "uploads": (old or {}).get("uploads", {})}


async def cmd_scan(args):
    async with client() as c:
        chan = await resolve_channel(c, channel_ref(args))
        st = Store(chan.id)
        m = st.m
        m["channel"] = getattr(chan, "title", None) or str(chan.id)

        found, group_cap, skipped, last_id = [], {}, {}, m["last_id"]
        async for msg in c.iter_messages(chan, reverse=True, min_id=m["last_id"]):
            last_id = max(last_id, msg.id)
            kind = classify(msg)
            if msg.grouped_id and msg.message:  # legenda do álbum + se o portador será enviado
                group_cap.setdefault(msg.grouped_id, (msg.message, msg.text, kind is not None))
            if kind:
                found.append((msg, kind))
            elif (not msg.media or isinstance(msg.media, MessageMediaWebPage)) and is_guide(msg.message):
                m["guides"][str(msg.id)] = guide_entry(msg, m["guides"].get(str(msg.id)))
            elif msg.media and not isinstance(msg.media, MessageMediaWebPage):
                lbl = skipped_label(msg)
                skipped[lbl] = skipped.get(lbl, 0) + 1

        # Guias antigos podem ter sido editados (novos módulos): rebusca pelo id.
        old_ids = [g["id"] for g in m["guides"].values() if g["id"] <= m["last_id"]]
        if old_ids:
            for gid, msg in zip(old_ids, await c.get_messages(chan, ids=old_ids)):
                old = m["guides"][str(gid)]
                if msg is None:
                    old["deleted"] = True
                else:
                    m["guides"][str(gid)] = guide_entry(msg, old)

        seq = max((v["seq"] for v in m["items"].values()), default=0)
        given = set()
        for msg, kind in found:
            cap, cap_html, inherited = msg.message, msg.text, False
            if not cap and msg.grouped_id in group_cap:  # álbum: a legenda fica em um item só
                cap, cap_html, carrier_sent = group_cap[msg.grouped_id]
                # portador fora do escopo (ex.: foto): o 1º item enviado do álbum leva a legenda
                inherited = carrier_sent or msg.grouped_id in given
                given.add(msg.grouped_id)
            seq += 1
            m["items"][str(msg.id)] = new_item(msg, kind, seq, cap, cap_html, inherited)

        mp = st.module_map()
        for v in m["items"].values():
            v["module"] = mp.get(v["tag"])
        m["last_id"] = last_id
        st.save()

    items = st.ordered()
    n_new = {k: sum(1 for _, kk in found if kk == k) for k in ("video", "document")}
    print(f"Canal: {m['channel']} (id {chan.id})")
    print(f"Novos nesta varredura: {len(found)} ({n_new['video']} vídeos, {n_new['document']} documentos)")
    print_inventory(st, items)
    if skipped:
        print("Ignorados (fora do escopo): " + ", ".join(f"{k} {n}" for k, n in skipped.items()))
    if out := safe_index(st):
        print(f"Índice: {out}")


def print_inventory(st, items):
    total = sum(v["size"] for v in items)
    done = [v for v in items if v["status"] == "done"]
    pending = sum(v["size"] for v in items) - sum(v["size"] for v in done)
    biggest = max(items, key=lambda v: v["size"], default=None)
    vids = sum(1 for v in items if v["kind"] == "video")
    print(f"Total: {len(items)} itens ({vids} vídeos, {len(items) - vids} documentos) | {fmt_size(total)}"
          + (f" | maior: {fmt_size(biggest['size'])}" if biggest else ""))
    print(f"Baixados: {len(done)} ({fmt_size(total - pending)}) | Pendentes: {len(items) - len(done)} ({fmt_size(pending)})")
    free = shutil.disk_usage(st.dir).free
    print(f"Disco livre em {st.dir}: {fmt_size(free)} " + ("✓" if free > pending * 1.05 else "⚠ insuficiente para o restante"))

    guides = [g for g in st.guides() if not g.get("deleted")]
    if guides:
        guide_tags = set(st.module_map())
        item_tags = {v["tag"] for v in items if v["tag"]}
        print(f"Guias de navegação: {len(guides)} (ids {', '.join(str(g['id']) for g in guides)}) | "
              f"itens com tag: {len(item_tags)}/{len(items)}")
        missing = sorted(guide_tags - item_tags)
        if missing:
            print(f"  ⚠ {len(missing)} tag(s) do guia sem arquivo correspondente: {' '.join(missing[:15])}"
                  + (" ..." if len(missing) > 15 else ""))
    else:
        print("Guias de navegação: nenhum encontrado (mensagem só de texto com "
              f">= {GUIDE_MIN_TAGS} hashtags)")
    big = [v for v in items if v["size"] > PROFILE["max_upload"]]
    if big:
        print(f"⚠ {len(big)} arquivo(s) acima do limite de upload desta conta "
              f"({fmt_size(PROFILE['max_upload'])}); o modo copy ainda funciona para eles.")


# --------------------------------------------------------------- download

async def download_one(c, chan, st, v, sem, run):
    async with sem:
        st.files.mkdir(parents=True, exist_ok=True)
        final = st.files / v["file"]
        part = st.files / (v["file"] + ".part")
        tag = f"[{v['seq']:04d}]"
        errors_left = MAX_ATTEMPTS

        while True:
            if run.get("abort"):
                v.update(status="pending", updated=now())
                break
            v.update(status="downloading", attempts=v["attempts"] + 1, updated=now())
            st.save()
            try:
                # Rebusca a mensagem a cada tentativa: renova o file_reference (que expira).
                doc = media_doc(await c.get_messages(chan, ids=v["id"]))
                if not doc:
                    raise RuntimeError("mensagem removida do canal de origem")
                size = doc.size
                if v.get("doc_id") not in (None, doc.id) or (part.exists() and part.stat().st_size > size):
                    print(f"{tag} mídia substituída na origem: recomeçando do zero")
                    part.unlink(missing_ok=True)
                v.update(doc_id=doc.id, size=size)
                offset = part.stat().st_size if part.exists() else 0
                offset -= offset % CHUNK  # retoma do último bloco completo
                if offset:
                    print(f"{tag} retomando de {fmt_size(offset)}")

                next_report = 0
                with open(part, "r+b" if part.exists() else "wb") as fh:
                    fh.seek(offset)
                    fh.truncate()
                    async for chunk in c.iter_download(doc, offset=offset, request_size=CHUNK,
                                                       file_size=size):
                        fh.write(chunk)
                        offset += len(chunk)
                        run["bytes"] += len(chunk)
                        pct = offset * 100 // size if size else 100
                        if pct >= next_report:
                            print(f"{tag} {pct:3d}%  {fmt_size(offset)} / {fmt_size(size)}  {v['title'][:50]}")
                            next_report = pct - pct % 10 + 10

                if part.stat().st_size != size:
                    raise IOError(f"tamanho divergente: {part.stat().st_size} != {size}")
                os.replace(part, final)
                caption_file(st, v).write_text(v["caption"], encoding="utf-8")
                v.update(status="done", error=None, updated=now())
                run["done"] += 1
                print(f"{tag} OK  {v['file']}")
                break

            except Exception as e:  # FloodWait, rede, file reference, disco...
                v["error"] = f"{type(e).__name__}: {e}"
                if isinstance(e, errors.FrozenMethodInvalidError):
                    run["abort"] = ("O Telegram restringiu esta conta (FROZEN_METHOD_INVALID). Verifique o "
                                    "app oficial; a ferramenta parou para não agravar a restrição.")
                    v.update(status="pending", updated=now())
                    break
                secs = getattr(e, "seconds", None) if isinstance(e, errors.FloodError) else None
                if secs is not None:  # espera pedida pelo Telegram: não conta como falha
                    hint = " (pode interromper com Ctrl+C e retomar depois)" if secs > LONG_FLOOD else ""
                    print(f"{tag} limite do Telegram: aguardando {secs}s{hint}")
                    await asyncio.sleep(secs + 5)
                    continue
                errors_left -= 1
                if errors_left == 0:
                    v.update(status="failed", updated=now())
                    run["failed"] += 1
                    run["failed_ids"].add(v["id"])
                    print(f"{tag} FALHOU: {v['error']}  (depois rode 'download --only-failed')")
                    break
                wait = min(10 * 2 ** (MAX_ATTEMPTS - errors_left - 1), 300)
                print(f"{tag} erro ({MAX_ATTEMPTS - errors_left}/{MAX_ATTEMPTS}): {v['error']} — nova tentativa em {wait}s")
                await asyncio.sleep(wait)
        st.save()

        rate = run["bytes"] / max(time.time() - run["t0"], 1)
        left = sum(x["size"] for x in run["scope"] if x["status"] != "done")
        if rate and left:
            print(f"      velocidade média {fmt_size(rate)}/s | restante {fmt_size(left)} | ETA ~{fmt_eta(left / rate)}")


def candidates(st, args, exclude=()):
    lo, hi = 1, 10 ** 9
    if args.range:
        a, _, b = args.range.partition("-")
        lo, hi = int(a), int(b or a)
    scope = [v for v in st.ordered() if lo <= v["seq"] <= hi]
    changed = False
    for v in scope:  # marcado como baixado mas o arquivo sumiu: volta para a fila
        if v["status"] == "done" and not (st.files / v["file"]).exists():
            print(f"[{v['seq']:04d}] arquivo ausente no disco, volta para pendente")
            v["status"], changed = "pending", True
    if changed:
        st.save()
    wanted = {"failed"} if args.only_failed else {"pending", "failed", "downloading"}
    # exclude = falhas desta execução: não são retentadas em todo lote (evita loop infinito)
    return scope, [v for v in scope if v["status"] in wanted and v["id"] not in exclude]


def take_batch(cands, n, mb):
    out, total = [], 0
    for v in cands:
        if out and (len(out) >= n or (mb and total + v["size"] > mb * 2**20)):
            break
        out.append(v)
        total += v["size"]
    return out


async def cmd_download(args):
    conc = args.concurrency or PROFILE["concurrency"]
    n = args.batch or PROFILE["batch"]
    pause = PROFILE["pause"] if args.pause is None else args.pause
    async with client() as c:
        chan = await resolve_channel(c, channel_ref(args))
        st = Store(chan.id)
        if not st.m["items"]:
            sys.exit("Manifest vazio. Rode 'scan' primeiro.")
        run = {"bytes": 0, "done": 0, "failed": 0, "failed_ids": set(), "t0": time.time()}
        while True:
            run["scope"], cands = candidates(st, args, run["failed_ids"])
            batch = take_batch(cands, n, args.batch_mb)
            if not batch:
                print("Nada pendente nessa seleção.")
                break
            need = sum(v["size"] for v in batch)
            free = shutil.disk_usage(st.dir).free
            if need > free * 0.95:
                sys.exit(f"Espaço insuficiente: lote precisa ~{fmt_size(need)}, livre {fmt_size(free)}")

            print(f"\n== Lote: {len(batch)} itens ({fmt_size(need)}), seq "
                  f"{batch[0]['seq']}–{batch[-1]['seq']}, concorrência {conc}")
            sem, t0, before = asyncio.Semaphore(conc), time.time(), (run["done"], run["failed"])
            await asyncio.gather(*(download_one(c, chan, st, v, sem, run) for v in batch))
            print(f"== Lote concluído em {fmt_eta(time.time() - t0)}: {run['done'] - before[0]} ok, "
                  f"{run['failed'] - before[1]} falha(s)")

            if run.get("abort"):
                print(f"\n⚠ {run['abort']}")
                break
            if not args.all or not candidates(st, args, run["failed_ids"])[1]:
                break
            print(f"Pausa de {pause}s entre lotes...")
            await asyncio.sleep(pause)
    if run["failed_ids"]:
        print(f"\n{len(run['failed_ids'])} item(ns) falharam nesta execução; tente depois com 'download --only-failed'.")
    print_summary(st)
    safe_index(st)


def print_summary(st):
    items = st.ordered()
    by = {}
    for v in items:
        by.setdefault(v["status"], []).append(v)
    print(f"\nCanal: {st.m['channel']}  |  {len(by.get('done', []))}/{len(items)} baixados")
    for s in ("pending", "downloading", "failed", "done"):
        if by.get(s):
            print(f"  {s:<12} {len(by[s]):>5}  {fmt_size(sum(v['size'] for v in by[s]))}")
    for v in by.get("failed", []):
        print(f"  ✗ [{v['seq']:04d}] {v['title'][:60]} — {v['error']}")
    dests = sorted({k for v in items for k in v["uploads"]})
    for d in dests:
        ok = sum(1 for v in items if v["uploads"].get(d, {}).get("status") == "done")
        bad = [v for v in items if v["uploads"].get(d, {}).get("error")]
        g = sum(1 for x in st.guides() if d in x["uploads"])
        print(f"  upload {d}: {ok}/{len(items)} enviados, guia: {'publicado' if g else 'pendente'}")
        for v in bad:
            print(f"    ✗ [{v['seq']:04d}] {v['uploads'][d]['status']}: {v['uploads'][d]['error']}")


async def cmd_status(args):
    st = offline_store(channel_ref(args), readonly=True)
    if args.list:
        for v in st.ordered():
            ups = ",".join(f"{u['status']}" for u in v["uploads"].values()) or "-"
            print(f"[{v['seq']:04d}] {v['kind'][:3]} {v['status']:<11} {fmt_size(v['size']):>9}  "
                  f"up:{ups:<6} {v['tag'] or '':<7} {v['title'][:60]}")
    print_summary(st)


# ------------------------------------------------------------------ índice

UNCLASSIFIED = "Sem classificação"


def md_escape(s):
    s = re.sub(r"([\\`*_\[\]<~])", r"\\\1", str(s))
    s = re.sub(r"(\s)(#+\s*)$", r"\1\\\2", s)  # '#' final: senão o cabeçalho o descarta
    return "\\" + s if s.startswith("#") else s


def md_code(s):
    s = str(s)
    fence = "`" * (max((len(r) for r in re.findall(r"`+", s)), default=0) + 1)
    return f"{fence} {s} {fence}" if "`" in s else f"`{s}`"


def item_path(v):
    """(cabeçalhos, título da linha) do item no índice."""
    path = v.get("path")
    if not isinstance(path, list):  # manifest antigo, sem 'path': calcula pela legenda
        path = parse_hierarchy(v.get("caption"))
    path = list(path)
    while path and not path[-1]:
        path.pop()
    if not path or not path[0]:
        if v.get("module"):  # sem módulo na legenda: usa o do guia
            path = [v["module"]] + path[1:]
        elif not path:
            return (UNCLASSIFIED,), v.get("title") or v.get("file")
    if len(path) >= LEAF_LEVEL:
        heads, leaf = path[:-1], path[-1]
    else:
        heads, leaf = path, v.get("title") or v.get("file")
    missing = {0: "(sem módulo)", 1: "(sem submódulo)"}
    return tuple(h or missing.get(i, f"(sem nível {i + 1})") for i, h in enumerate(heads)), leaf


def write_index_md(st):
    """INDICE.md: hierarquia módulo > submódulo > aula, na ordem do canal, com [x] = baixado."""
    items = st.ordered()
    vids = sum(1 for v in items if v["kind"] == "video")
    done = sum(1 for v in items if v["status"] == "done")
    lines = [f"# Índice — {md_escape(st.m.get('channel') or st.m.get('channel_id'))}",
             f"_Gerado em {datetime.now().astimezone():%d/%m/%Y %H:%M} · {len(items)} itens "
             f"({vids} vídeos, {len(items) - vids} documentos) · {done} baixados_"]
    prev = ()
    for v in items:
        heads, leaf = item_path(v)
        i = 0
        while i < min(len(heads), len(prev)) and heads[i] == prev[i]:
            i += 1
        if i == len(heads) and len(heads) < len(prev):
            i -= 1  # voltou a um nível acima: repete o cabeçalho para não ficar sob o anterior
        if i < len(heads):
            lines.append("")
            lines += ["#" * min(k + 2, 6) + " " + md_escape(h) for k, h in enumerate(heads) if k >= i]
        prev = heads
        tag = f"{md_code(v['tag'])} " if v.get("tag") else ""
        lines.append(f"- [{'x' if v['status'] == 'done' else ' '}] {tag}{md_escape(leaf)} — {md_code(v['file'])}")
    out = st.dir / INDEX_FILE
    tmp = st.dir / f".{INDEX_FILE}.{os.getpid()}.tmp"
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)  # falha no meio: não deixa .tmp órfão
    return out


def safe_index(st):
    """Gera o INDICE.md sem nunca interromper o comando que o chamou."""
    try:
        return write_index_md(st)
    except Exception as e:
        print(f"⚠ índice não gerado: {type(e).__name__}: {e}")
        return None


async def cmd_index(args):
    st = offline_store(channel_ref(args), readonly=True)
    out = safe_index(st)
    if not out:
        sys.exit(1)
    print(f"Índice gerado: {out}")


# ----------------------------------------------------------------- upload

def progress_printer(tag):
    state = {"next": 0}

    def cb(sent, total):
        pct = sent * 100 // total if total else 100
        if pct >= state["next"]:
            print(f"{tag} upload {pct:3d}%  {fmt_size(sent)} / {fmt_size(total)}")
            state["next"] = pct - pct % 10 + 10
    return cb


def make_thumb(path, out):
    """Miniatura opcional via ffmpeg (sem ela o Telegram pode exibir o vídeo sem preview)."""
    if not shutil.which("ffmpeg"):
        return None
    out.unlink(missing_ok=True)  # nunca reaproveita a miniatura de outro vídeo
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", "1", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-q:v", "5", str(out)], check=True, timeout=60, stdin=subprocess.DEVNULL)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:
        return None


def caption_plan(v):
    """(legenda na mídia, texto separado ou None) conforme o limite da conta."""
    cap = "" if v.get("caption_inherited") else v["caption_html"]
    if cap and tg_len(v["caption"]) > CAPTION_LIMIT:
        return "", cap
    return cap, None


async def flood_retry(fn, tag=""):
    """Executa fn(); FloodWait curto: espera e repete. Longo ou sem tempo: propaga."""
    while True:
        try:
            return await fn()
        except errors.FloodError as e:
            secs = getattr(e, "seconds", None)
            if secs is None or secs > LONG_FLOOD:
                raise
            print(f"{tag} limite do Telegram: aguardando {secs}s")
            await asyncio.sleep(secs + 5)


async def post_media(c, target, v, media, st):
    """Envia só a mídia (copy: Document da origem; upload: Path local) com a legenda que couber."""
    caption, _ = caption_plan(v)
    kw = dict(caption=caption, parse_mode="html")
    thumb = None
    if isinstance(media, Path):
        attrs = [DocumentAttributeFilename(v["orig_name"])]
        if v["kind"] == "video":
            attrs.append(DocumentAttributeVideo(duration=v["duration"] or 0, w=v["width"] or 0,
                                                h=v["height"] or 0, supports_streaming=True))
            thumb = make_thumb(media, st.dir / f"thumb_{v['seq']:04d}.jpg")
        kw.update(attributes=attrs, force_document=v["kind"] == "document", mime_type=v.get("mime") or None,
                  supports_streaming=v["kind"] == "video", thumb=str(thumb) if thumb else None,
                  progress_callback=progress_printer(f"[{v['seq']:04d}]"))
        media = str(media)
    try:
        return (await c.send_file(target, media, **kw)).id
    finally:
        if thumb:
            thumb.unlink(missing_ok=True)


async def send_media(c, chan, target, v, st, mode):
    reason = None
    if mode in ("auto", "copy"):
        doc = media_doc(await c.get_messages(chan, ids=v["id"]))  # file_reference fresco
        if doc:
            try:
                return await post_media(c, target, v, doc, st), "copy"
            except COPY_ERRORS as e:  # só erros da cópia caem para upload; os do destino propagam
                reason = f"cópia recusada: {type(e).__name__}"
                if mode == "copy":
                    raise
        else:
            reason = "mensagem removida da origem"
            if mode == "copy":
                raise RuntimeError(reason)

    path = st.files / v["file"]
    if v["status"] != "done" or not path.exists():
        raise NotReady(reason or "ainda não baixado")
    if v["size"] > PROFILE["max_upload"]:
        raise errors.BadRequestError(None, f"arquivo maior que o limite de upload da conta "
                                           f"({fmt_size(PROFILE['max_upload'])})")
    return await post_media(c, target, v, path, st), "upload"


async def publish_guides_tg(c, target, st, key):
    for g in st.guides():
        if g.get("deleted"):
            continue
        up = g["uploads"].get(key)
        if up and up.get("hash") == g["hash"]:
            continue
        msg_id, action = None, "publicado"
        if up and up.get("msg_id"):
            try:
                await flood_retry(lambda: c.edit_message(target, up["msg_id"], g["text_html"],
                                                         parse_mode="html", link_preview=False))
                msg_id, action = up["msg_id"], "atualizado"
            except errors.MessageNotModifiedError:
                msg_id = up["msg_id"]
            except errors.MessageIdInvalidError:
                msg_id = None  # guia apagado no destino: publica de novo
        if msg_id is None:
            msg = await flood_retry(lambda: c.send_message(target, g["text_html"], parse_mode="html",
                                                           link_preview=False))
            msg_id = msg.id
            g["uploads"][key] = {"status": "done", "msg_id": msg_id, "hash": g["hash"], "updated": now()}
            st.save()  # grava antes de fixar: falha no pin nunca republica o guia
            try:
                await c.pin_message(target, msg, notify=False)
            except errors.RPCError as e:
                print(f"  (não foi possível fixar o guia: {type(e).__name__})")
        g["uploads"][key] = {"status": "done", "msg_id": msg_id, "hash": g["hash"], "updated": now()}
        st.save()
        print(f"Guia {g['id']} {action} no destino (msg {msg_id})")


def mark_upload_error(st, v, key, err):
    up = v["uploads"].get(key)
    if up and up.get("status") == "media_sent":
        up["error"] = err  # mídia já está no destino: mantém, falta só a legenda
    else:
        v["uploads"][key] = {"status": "failed", "error": err, "updated": now()}
    st.save()


async def upload_telegram(c, chan, st, key, where, args):
    target = await get_entity(c, where)
    items = st.ordered()
    if not any(key in v["uploads"] for v in items) and not args.allow_nonempty:
        existing = [x for x in await c.get_messages(target, limit=5) if not x.action]
        if existing:
            sys.exit("O destino já tem mensagens. O plano prevê um canal vazio e dedicado; "
                     "use --allow-nonempty para enviar mesmo assim.")

    sent = 0
    for v in items:
        if v["uploads"].get(key, {}).get("status") == "done":
            continue
        if args.limit and sent >= args.limit:
            break
        tag = f"[{v['seq']:04d}]"
        errors_left = 3
        while True:
            up = v["uploads"].get(key, {})
            try:
                if up.get("status") != "media_sent":
                    print(f"{tag} enviando {v['file']}")
                    mid, how = await flood_retry(lambda: send_media(c, chan, target, v, st, args.mode), tag)
                    up = {"status": "media_sent", "via": how, "msg_ids": [mid], "updated": now()}
                    v["uploads"][key] = up
                    st.save()  # persiste antes da legenda: nova tentativa nunca reenvia a mídia
                _, text = caption_plan(v)
                if text:
                    t = await flood_retry(lambda: c.send_message(target, text, parse_mode="html",
                                                                 link_preview=False), tag)
                    up["msg_ids"].append(t.id)
                up.update(status="done", error=None, updated=now())
                st.save()
                print(f"{tag} OK ({up['via']})")
                sent += 1
                break
            except NotReady as e:
                print(f"Parando em {tag} para manter a ordem: {e}. Baixe com: download --range {v['seq']}")
                return sent, False
            except errors.FloodError as e:
                secs = getattr(e, "seconds", None)
                mark_upload_error(st, v, key, f"{type(e).__name__} {secs or ''}s")
                wait = f"aguarde {fmt_eta(secs)} e " if secs else ""
                sys.exit(f"{tag} limite do Telegram ({type(e).__name__}): {wait}rode de novo; continua daqui.")
            except (errors.BadRequestError, errors.ForbiddenError) as e:  # determinístico: repetir não resolve
                err = f"{type(e).__name__}: {e}"
                mark_upload_error(st, v, key, err)
                hint = (" Reduza TG_CAPTION_LIMIT no .env." if isinstance(e, errors.MediaCaptionTooLongError)
                        else " Confira se você é admin com permissão de postar no destino."
                        if isinstance(e, errors.ForbiddenError) else "")
                sys.exit(f"{tag} falha no upload: {err}.{hint}")
            except Exception as e:  # rede/servidor: tenta de novo
                errors_left -= 1
                err = f"{type(e).__name__}: {e}"
                if errors_left == 0:
                    mark_upload_error(st, v, key, err)
                    sys.exit(f"{tag} falha no upload: {err}. Rode de novo para retomar daqui.")
                print(f"{tag} erro: {err} — nova tentativa em 15s")
                await asyncio.sleep(15)
        if args.pause:
            await asyncio.sleep(args.pause)

    pending = [v for v in items if v["uploads"].get(key, {}).get("status") != "done"]
    if pending:
        print(f"{len(pending)} item(ns) ainda não enviados; o guia é publicado quando todos forem enviados.")
        return sent, False
    try:
        await publish_guides_tg(c, target, st, key)
    except errors.RPCError as e:
        sys.exit(f"Falha ao publicar o guia: {type(e).__name__}: {e}. Rode 'upload' de novo.")
    return sent, True


def put_file(kind, where, src):
    if kind == "local":
        d = Path(where).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d / src.name)
    else:  # rclone
        base = where if where.endswith((":", "/")) else where + "/"
        subprocess.run(["rclone", "copyto", str(src), base + src.name], check=True,
                       stdin=subprocess.DEVNULL)


def write_guide_index(st):
    """Equivalente em arquivo das hashtags clicáveis: guia original + índice tag -> arquivo."""
    lines = [f"GUIA DE NAVEGAÇÃO — {st.m['channel']}", ""]
    for g in st.guides():
        if not g.get("deleted"):
            lines += [g["text"], "", "-" * 60, ""]
    lines += ["ÍNDICE (tag -> arquivo)", ""]
    current = object()
    for v in st.ordered():
        if v["module"] != current:
            current = v["module"]
            lines += ["", f"[{current or 'sem módulo'}]"]
        lines.append(f"  {v['tag'] or '-':<8} {v['file']}")
    out = st.dir / GUIDE_FILE
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def upload_files(st, kind, where, key, args):
    sent, not_ready = 0, 0
    for v in st.ordered():
        if v["uploads"].get(key, {}).get("status") == "done":
            continue
        src = st.files / v["file"]
        if v["status"] == "done" and not src.exists():
            print(f"[{v['seq']:04d}] arquivo ausente no disco: volta para pendente (rode 'download')")
            v["status"] = "pending"
            st.save()
        if v["status"] != "done":
            not_ready += 1
            continue
        if args.limit and sent >= args.limit:
            break
        print(f"[{v['seq']:04d}] enviando {v['file']}")
        try:
            put_file(kind, where, src)
            if caption_file(st, v).exists():
                put_file(kind, where, caption_file(st, v))
        except Exception as e:
            v["uploads"][key] = {"status": "failed", "error": f"{type(e).__name__}: {e}", "updated": now()}
            st.save()
            sys.exit(f"[{v['seq']:04d}] falha no upload: {e}. Rode de novo para retomar.")
        v["uploads"][key] = {"status": "done", "updated": now()}
        st.save()
        sent += 1
    put_file(kind, where, write_guide_index(st))
    idx = safe_index(st)
    if idx:
        try:
            put_file(kind, where, idx)
        except Exception as e:  # best-effort: não afeta o upload dos arquivos
            print(f"⚠ índice não copiado para o destino: {type(e).__name__}: {e}")
    for g in st.guides():
        g["uploads"][key] = {"status": "done", "updated": now()}
    st.save()
    if not_ready:
        print(f"{not_ready} item(ns) ainda não baixados foram pulados (a numeração no nome mantém a ordem).")
    return sent


async def cmd_upload(args):
    dest = args.to or os.getenv("DEST_DEFAULT")
    kind, _, where = (dest or "").partition(":")
    if kind not in ("telegram", "rclone", "local") or not where:
        sys.exit("Informe --to telegram:<chat> | rclone:<remote:pasta> | local:<pasta> (ou DEST_DEFAULT no .env)")
    if kind == "rclone" and ":" not in where:
        sys.exit("Destino rclone precisa do remote, ex.: rclone:gdrive:Cursos/Canal")
    if kind == "rclone" and not shutil.which("rclone"):
        sys.exit("rclone não encontrado. Instale (https://rclone.org/install/) e rode 'rclone config'.")

    ref = channel_ref(args)
    if kind == "telegram":
        async with client() as c:
            chan = await resolve_channel(c, ref)
            st = Store(chan.id)
            sent, _ = await upload_telegram(c, chan, st, dest, where, args)
    else:
        st = offline_store(ref)
        sent = upload_files(st, kind, where, dest, args)
    print(f"Upload: {sent} item(ns) enviados para {dest}")


# -------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--channel", help="canal de origem (padrão: TG_CHANNEL do .env)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="autentica a conta")
    sub.add_parser("chats", help="lista canais/grupos e IDs")
    sub.add_parser("scan", parents=[common], help="inventário incremental do canal")

    d = sub.add_parser("download", parents=[common], help="baixa em lotes")
    d.add_argument("--batch", type=int, help=f"itens por lote (padrão {PROFILE['batch']})")
    d.add_argument("--batch-mb", type=int, help="limite de MB por lote (fecha no que vier primeiro)")
    d.add_argument("--concurrency", type=int, help=f"downloads simultâneos (padrão {PROFILE['concurrency']}, sugerido <= 4)")
    d.add_argument("--all", action="store_true", help="processa lotes em sequência até terminar")
    d.add_argument("--pause", type=int, help=f"pausa entre lotes em s (padrão {PROFILE['pause']})")
    d.add_argument("--range", help="faixa de seq, ex.: 1-50 ou 51")
    d.add_argument("--only-failed", action="store_true", help="apenas os que falharam")

    s = sub.add_parser("status", parents=[common], help="resumo e falhas")
    s.add_argument("--list", action="store_true", help="lista todos os itens")

    u = sub.add_parser("upload", parents=[common], help="reenvia em ordem para o destino")
    u.add_argument("--to", help="telegram:<chat> | rclone:<remote:pasta> | local:<pasta> (padrão: DEST_DEFAULT)")
    u.add_argument("--mode", choices=("auto", "copy", "upload"), default="auto",
                   help="auto: copia no servidor e, se recusado, envia o arquivo baixado (padrão)")
    u.add_argument("--limit", type=int, default=0, help="máximo de itens nesta execução")
    u.add_argument("--pause", type=int, default=3, help="pausa entre envios no Telegram em s (padrão 3)")
    u.add_argument("--allow-nonempty", action="store_true", help="permite destino que já tem mensagens")
    sub.add_parser("index", parents=[common], help="regenera o INDICE.md (offline)")

    args = p.parse_args()
    try:
        asyncio.run(globals()[f"cmd_{args.cmd}"](args))
    except KeyboardInterrupt:
        print("\nInterrompido. Rode o mesmo comando de novo para retomar de onde parou.")


if __name__ == "__main__":
    main()
