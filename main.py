# -*- coding: utf-8 -*-
"""B站"看一眼"：把视频的文字素材和几张关键帧原样交给模型，不下判断。

为什么单独做一个
----------------
    bilibili_ai_bot 的 watch_video 是"取素材 + 评一次分 + 写记忆"一体：
    素材被它自己的 LLM 消化成一段描述，再在一间没有上下文的空房间里打分、
    写"我的感想"。所以那段感想天然端着——不是她说的，是插件替她说的。

    这里只做前半截：把标题、简介、热评、关键帧**原样**塞回她自己的上下文。
    读的是她，判断的是她，说不说也是她。

怎么拿到画面而不下载视频
------------------------
    B站有 storyboard 接口 `x/player/videoshot`，一次请求返回整片的关键帧
    拼图（10×10 网格，单帧 480×270）。本地裁切即可，不碰 yt-dlp、不碰
    ffmpeg、不碰视觉模型——她本来就是看图的那个。

关于登录态
----------
    游客只能拿到 3 条热评，带了 SESSDATA 才是 20 条。所以这里会先用自己
    配置里的 sessdata；没填就去读 bilibili_ai_bot 的那份配置（同一台机器
    同一个账号）。都拿不到就退回游客，功能不因此失败。

    （弹幕接口现在对谁都返回加密数据，解不开，所以干脆不做——热评已经够
    看出片子的调性，画面又有关键帧。）

不做的事
--------
    不打分、不写记忆、不点赞不评论、不发视频到聊天里。要互动她自己调
    bili_action，要说给谁听她自己开口。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_bili_reader"

API_VIEW = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
API_SHOT = "https://api.bilibili.com/x/player/videoshot?bvid={bvid}&cid={cid}&index=1"
API_REPLY = "https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps={ps}&pn=1"
API_MSG_REPLY = "https://api.bilibili.com/x/msgfeed/reply?platform=web&build=0&mobi_app=web"
ARCHIVE_URL = "https://aigengtu.com/archive?p={page}"
PAGE_RE = re.compile(r"第\s*\d+\s*/\s*(\d+)\s*页")
CARD_RE = re.compile(
    r'<div class="ar-card">.*?href="/zh/meme/(\d+)" title="([^"]*)".*?'
    r'src="(https://img\.aigengtu\.com/0_preview/meme/\d+\.webp)"',
    re.S,
)


def fetch_archive_page(page: int = 1) -> tuple[list[dict], int]:
    """档案馆一页的卡片 + 总页数。"""
    html = _get(ARCHIVE_URL.format(page=max(1, int(page)))).decode("utf-8", "ignore")
    total = 0
    m = PAGE_RE.search(html)
    if m:
        total = int(m.group(1))
    out = []
    for num, title, thumb in CARD_RE.findall(html):
        out.append({
            "num": num,
            "title": title.strip(),
            "thumb": thumb,
            "full": f"https://img.aigengtu.com/meme/{num}.webp",
        })
    return out, total


def build_sticker_index() -> dict:
    """把整站 36 页扫一遍，落一个 json，之后关键词就能全站查。"""
    cards: dict[str, dict] = {}
    total_pages = 1
    page = 1
    while page <= min(total_pages, 60):
        try:
            batch, total = fetch_archive_page(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili-read] 索引第 {page} 页失败: {e}")
            break
        total_pages = total or total_pages
        if not batch:
            break
        for c in batch:
            cards[c["num"]] = c
        page += 1
    data = {
        "built_at": int(time.time()),
        "total_pages": total_pages,
        "count": len(cards),
        "cards": sorted(cards.values(), key=lambda x: int(x["num"])),
    }
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bili-read] 索引落盘失败: {e}")
    return data


def load_sticker_index(max_age_days: int = 7) -> dict:
    try:
        d = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if time.time() - int(d.get("built_at") or 0) > max_age_days * 86400:
            return {}
        return d
    except Exception:  # noqa: BLE001
        return {}


def load_vision_tags() -> dict:
    """视觉模型打的标签（tools/tag_stickers.py 产出，给 sticker_pack 用同一份）。"""
    try:
        p = ROOT / "plugin_data" / "astrbot_plugin_sticker_pack" / "vision_tags.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def search_local_archive(keyword: str) -> tuple[list[dict], Path | None]:
    """在本地馆藏里按标签/描述找。返回 (命中列表, 命中第一条的文件路径)。

    优先用视觉标签（细到「手指抵唇」「蒙眼对比」这种），其次手写索引。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return [], None
    vis = load_vision_tags()
    try:
        idx = json.loads(LOCAL_INDEX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        idx = {}

    hits = []
    for f in sorted(LOCAL_IMAGES.glob("*")):
        if f.suffix.lower() not in (".webp", ".png", ".jpg", ".jpeg", ".gif"):
            continue
        v = vis.get(f.name) or {}
        tags = v.get("tags") or []
        desc = str(v.get("desc") or "")
        if not tags and not desc:
            for k, val in idx.items():
                if Path(str(k)).stem == f.stem:
                    tags = val.get("tags") or []
                    desc = str(val.get("desc") or "")
                    break
        if kw in desc or any(kw in str(t) for t in tags):
            hits.append({
                "file": f,
                "num": f.stem[:8],
                "title": desc or "、".join(tags[:3]),
                "tags": tags,
            })
    return hits, (hits[0]["file"] if hits else None)


def convert_image(src: str | Path, out_dir: Path, tag: str) -> Path | None:
    """本地/网络图片统一转成 jpg（webp、带透明、gif 都吃），B站 只收 jpg/png。"""
    try:
        from io import BytesIO

        from PIL import Image

        if isinstance(src, Path) or str(src).startswith(("C:", "c:", "/")):
            raw = Path(src).read_bytes()
        else:
            raw = _get(str(src), timeout=25)
        img = Image.open(BytesIO(raw))
        if getattr(img, "is_animated", False):
            img.seek(0)
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        if max(bg.size) > 1200:
            bg.thumbnail((1200, 1200))
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{tag}.jpg"
        bg.save(p, "JPEG", quality=88)
        return p
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bili-read] 存图失败: {e}")
        return None


def download_sticker(url: str, out_dir: Path, tag: str) -> Path | None:
    """保留旧名字，内部走统一转换。"""
    return convert_image(url, out_dir, tag)


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
ROOT = Path(__file__).resolve().parent.parent.parent
STICKER_DIR = ROOT / "temp" / "bili_stickers"
INDEX_PATH = STICKER_DIR / "index.json"
# 本地馆藏：sticker_pack 的 archive_dir，270 张，每张带 tags + desc
# 注意 ROOT 是 AstrBot/data，所以往上两级才是 QQBot
LOCAL_ARCHIVE = ROOT.parent.parent / "blue-fish-archive"
LOCAL_INDEX = LOCAL_ARCHIVE / "archive_index.json"
LOCAL_IMAGES = LOCAL_ARCHIVE / "large"

DEFAULT_CONFIG = {
    "enabled": True,
    "admin_only": True,
    "sessdata": "",
    "daily_limit": 12,
    "max_frames": 9,
    "max_comments": 20,
    "desc_chars": 300,
    # 有人回她 → 叫醒她（不是自动回复，只是递个信）
    "notify_enabled": True,
    "notify_umo": "",
    "notify_poll_minutes": 15,
    "notify_gap_minutes": 60,
    "notify_daily_wakes": 8,
}


def fetch_msg_replies(cookie: str) -> list[dict]:
    """谁回了她。返回按时间倒序的条目。"""
    d = _get_json(API_MSG_REPLY, cookie)
    if d.get("code") != 0:
        return []
    out = []
    for it in ((d.get("data") or {}).get("items") or []):
        try:
            item = it.get("item") or {}
            user = it.get("user") or {}
            out.append({
                "id": int(it.get("id") or 0),
                "time": int(it.get("reply_time") or 0),
                "user": str(user.get("nickname") or "?"),
                "mid": str(user.get("mid") or ""),
                "text": str(item.get("source_content") or "").strip(),
                "mine": str(item.get("root_reply_content") or "").strip(),
                "uri": str(item.get("uri") or ""),
                "aid": str(item.get("subject_id") or ""),
                "root": str(item.get("root_id") or ""),
                "parent": str(item.get("source_id") or ""),
            })
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x["time"], reverse=True)
    return out



def parse_bvid(text: str) -> str:
    """从 URL / BV 号 / 一堆字里挑出 BV 号。"""
    m = BVID_RE.search(str(text or ""))
    return m.group(1) if m else ""


def _borrow_cookie() -> str:
    """借用 bilibili_ai_bot 的登录态（同一台机器、同一个账号）。"""
    try:
        p = ROOT / "config" / "astrbot_plugin_bilibili_ai_bot_config.json"
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        s, j, u = d.get("SESSDATA", ""), d.get("BILI_JCT", ""), d.get("DEDE_USER_ID", "")
        if s:
            return f"SESSDATA={s}; bili_jct={j}; DedeUserID={u}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[bili-read] 没借到登录态: {e}")
    return ""


def _headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept-Encoding": "identity",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def _get(url: str, cookie: str = "", timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=_headers(cookie))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, cookie: str = "") -> dict:
    return json.loads(_get(url, cookie).decode("utf-8", "ignore"))


def resolve_bvid(text: str, cookie: str = "") -> str:
    """BV 号直接返回；b23.tv 短链跟一次跳转再认。"""
    bv = parse_bvid(text)
    if bv:
        return bv
    short = re.search(r"(https?://b23\.tv/[0-9A-Za-z]+)", str(text or ""))
    if not short:
        return ""
    try:
        req = urllib.request.Request(short.group(1), headers=_headers(cookie))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as r:
            return parse_bvid(r.geturl())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[bili-read] 短链解析失败: {e}")
        return ""


def fetch_view(bvid: str, cookie: str) -> dict:
    d = _get_json(API_VIEW.format(bvid=bvid), cookie)
    if d.get("code") != 0:
        raise RuntimeError(f"{d.get('code')} {d.get('message')}")
    return d.get("data") or {}


def fetch_shot(bvid: str, cid: int, cookie: str) -> dict:
    try:
        d = _get_json(API_SHOT.format(bvid=bvid, cid=cid), cookie)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[bili-read] 关键帧接口失败: {e}")
        return {}
    return (d.get("data") or {}) if d.get("code") == 0 else {}


def fetch_comments(aid: int, ps: int, cookie: str) -> list[dict]:
    try:
        d = _get_json(API_REPLY.format(aid=aid, ps=max(1, min(20, ps))), cookie)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[bili-read] 热评接口失败: {e}")
        return []
    if d.get("code") != 0:
        return []
    out = []
    for r in ((d.get("data") or {}).get("replies") or [])[:ps]:
        try:
            out.append({
                "user": (r.get("member") or {}).get("uname", "?"),
                "text": (r.get("content") or {}).get("message", ""),
                "like": r.get("like", 0),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def crop_frames(shot: dict, count: int, out_dir: Path, tag: str, cookie: str) -> list[Path]:
    """从关键帧拼图里按时间顺序均匀裁 count 张。"""
    urls = (shot or {}).get("image") or []
    if not urls:
        return []
    cols = int(shot.get("img_x_len") or 10)
    rows = int(shot.get("img_y_len") or 10)
    fw = int(shot.get("img_x_size") or 0)
    fh = int(shot.get("img_y_size") or 0)
    total = len(shot.get("index") or [])
    if not (cols and rows and fw and fh):
        return []

    url = urls[0]
    if url.startswith("//"):
        url = "https:" + url
    try:
        from io import BytesIO

        from PIL import Image

        sheet = Image.open(BytesIO(_get(url, cookie, timeout=30))).convert("RGB")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[bili-read] 关键帧拼图打不开: {e}")
        return []

    capacity = min(cols * rows, total or cols * rows)
    count = max(1, min(count, capacity))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(count):
        idx = int(i * (capacity - 1) / (count - 1)) if count > 1 else 0
        r, c = divmod(idx, cols)
        box = (c * fw, r * fh, c * fw + fw, r * fh + fh)
        if box[2] > sheet.size[0] or box[3] > sheet.size[1]:
            continue
        p = out_dir / f"{tag}_{i + 1}.jpg"
        try:
            sheet.crop(box).save(p, "JPEG", quality=82)
            paths.append(p)
        except Exception:  # noqa: BLE001
            continue
    return paths


def _human(num) -> str:
    try:
        n = int(num)
    except (TypeError, ValueError):
        return "?"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _mmss(sec) -> str:
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


class BiliReadPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            self.cfg.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})
        self._data = ROOT / "plugin_data" / PLUGIN_NAME
        self._data.mkdir(parents=True, exist_ok=True)
        self._counter = self._data / "counter.json"

    async def initialize(self) -> None:
        owns = bool(str(self._cfg("sessdata") or "").strip())
        borrowed = bool(_borrow_cookie())
        self._notify_state = self._data / "notify_state.json"
        self._notify_task = None
        logger.info(
            f"[bili-read] 就绪：每天 {self._cfg('daily_limit')} 次、"
            f"默认 {self._cfg('max_frames')} 帧"
            f"｜登录态 {'自有' if owns else ('借用 bilibili_ai_bot' if borrowed else '游客（热评只有 3 条）')}"
        )
        if self._cfg("notify_enabled"):
            self._notify_task = asyncio.create_task(self._notify_loop())
            logger.info(
                f"[bili-read] 回信提醒已开：每 {self._cfg('notify_poll_minutes')} 分钟看一次，"
                f"至少隔 {self._cfg('notify_gap_minutes')} 分钟才叫她，一天最多 {self._cfg('notify_daily_wakes')} 次"
            )

    async def terminate(self) -> None:
        t = getattr(self, "_notify_task", None)
        if t and not t.done():
            t.cancel()

    # ---------------- 回信提醒 ----------------

    def _load_notify_state(self) -> dict:
        try:
            return json.loads(self._notify_state.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_notify_state(self, st: dict) -> None:
        try:
            self._notify_state.parent.mkdir(parents=True, exist_ok=True)
            self._notify_state.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili-read] 存提醒状态失败（会影响去重）: {e}")

    def _notify_umo(self) -> str:
        umo = str(self._cfg("notify_umo") or "").strip()
        if umo:
            return umo
        try:
            p = ROOT / "config" / "astrbot_plugin_bilibili_ai_bot_config.json"
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            return str(d.get("OWNER_QQ_UMO") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    async def _notify_loop(self) -> None:
        # 第一次先记水位，不叫醒她
        try:
            await asyncio.sleep(20)
            items = await asyncio.to_thread(fetch_msg_replies, self._cookie())
            if items:
                st = self._load_notify_state()
                st["last_id"] = max(st.get("last_id", 0), items[0]["id"])
                self._save_notify_state(st)
                logger.info(f"[bili-read] 回信水位初始化：{items[0]['id']}（不回补历史）")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[bili-read] 水位初始化失败: {e}")

        while True:
            try:
                await asyncio.sleep(max(3, int(self._cfg("notify_poll_minutes") or 15)) * 60)
                await self._check_new_replies()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[bili-read] 回信巡检出错: {e}")
                await asyncio.sleep(120)

    async def _check_new_replies(self) -> None:
        if not self._cfg("notify_enabled"):
            return
        items = await asyncio.to_thread(fetch_msg_replies, self._cookie())
        if not items:
            return
        st = self._load_notify_state()
        last_id = int(st.get("last_id") or 0)
        fresh = [x for x in items if x["id"] > last_id]
        if not fresh:
            return

        now = time.time()
        gap = max(5, int(self._cfg("notify_gap_minutes") or 60)) * 60
        cap = int(self._cfg("notify_daily_wakes") or 8)
        today = time.strftime("%Y-%m-%d")
        wakes = int(st["wakes"]) if st.get("wake_date") == today else 0

        st["last_id"] = max(x["id"] for x in fresh)
        st["pending"] = (st.get("pending") or []) + fresh
        st["pending"] = st["pending"][-20:]

        if now - float(st.get("last_wake") or 0) < gap or wakes >= cap:
            self._save_notify_state(st)
            logger.info(f"[bili-read] 有 {len(fresh)} 条新回复，先攒着（冷却/配额）")
            return

        pending = st.get("pending") or []
        umo = self._notify_umo()
        mgr = getattr(self.context, "cron_manager", None)
        if not umo or mgr is None:
            self._save_notify_state(st)
            logger.warning("[bili-read] 想叫她但没配 notify_umo / 没有 cron_manager，攒着不发")
            return

        lines = [f"有人回了你 {len(pending)} 条："]
        for i, x in enumerate(pending[-5:], 1):
            t = str(x.get("text") or "").replace("\n", " ")[:80]
            lines.append(f"{i}. {x.get('user')}：{t}")
        if len(pending) > 5:
            lines.append(f"……还有 {len(pending) - 5} 条更早的")
        lines.append("\n要接就自己接，不想理就算了。想看全部就调 bili_replies。")
        note = "\n".join(lines)

        try:
            await mgr.add_active_job(
                name="bili-replies",
                cron_expression=None,
                payload={
                    "session": umo,
                    "note": note,
                    "origin": "bili_reader",
                    # 上游靠这个字段判 role（cron/manager.py）：不传就是 member，
                    # 那一轮她自己的 admin 闸和本地工具会被全部挡掉。
                    "sender_id": umo.rsplit(":", 1)[-1] if ":" in umo else "",
                },
                description=note,
                timezone=None,
                persistent=False,
                run_once=True,
                run_at=datetime.now().astimezone() + timedelta(seconds=30),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili-read] 叫醒失败: {e}")
            self._save_notify_state(st)
            return

        st["pending"] = []
        st["last_wake"] = now
        st["wake_date"] = today
        st["wakes"] = wakes + 1
        self._save_notify_state(st)
        logger.info(f"[bili-read] 🔔 有人回她（{len(pending)} 条）→ 已叫醒，今天第 {wakes + 1} 次")

    # ---------------- 工具 ----------------

    @filter.llm_tool(name="bili_sticker")
    async def bili_sticker(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        page: float = 1,
        num: str = "",
    ):
        """从档案馆捞一张图，存成本地文件，给你发评论时带上去。

        档案馆就是那个「蓝色大肥鱼档案馆」，两千多张。挑好之后把返回的
        路径塞进 bili_action(action=comment, image=路径)。

        Args:
            keyword (string): 想找什么，比如「白饭」「生气」「睡觉」；留空就随便给一张
            page (number): 从第几页找，默认 1（每页约 65 张，共 36 页）
            num (string): 直接指定编号，比如 066；填了就跳过搜索
        Returns:
            str: 图片本地路径 + 它叫什么
        """
        if not self._cfg("enabled"):
            return "这个工具关着。"
        if not self._allowed(event):
            return "这个工具只在管理员私聊里能用。"

        out_dir = ROOT / "temp" / "bili_stickers"

        # 直接按编号取
        if str(num or "").strip():
            n = re.sub(r"\D", "", str(num))[:4].zfill(3)
            p = await asyncio.to_thread(
                download_sticker, f"https://img.aigengtu.com/meme/{n}.webp", out_dir, f"n{n}"
            )
            if not p:
                return f"没拿到 #{n}，可能编号不对。"
            return f"图存好了：{p}\n（#{n}，直接放进 bili_action 的 image 参数）"

        kw = str(keyword or "").strip()

        # ① 先翻本地馆藏（快、有中文标签）
        if kw:
            local_hits, _ = await asyncio.to_thread(search_local_archive, kw)
            if local_hits:
                pick = local_hits[0]
                p = await asyncio.to_thread(
                    convert_image, pick["file"], out_dir, f"n{pick['num']}"
                )
                if p:
                    tags = "、".join(pick["tags"][:5])
                    more = ""
                    if len(local_hits) > 1:
                        more = f"\n本地还有 {len(local_hits) - 1} 张同类的"
                    return (
                        f"{pick['title']}\n"
                        f"标签：{tags}{more}\n"
                        f"图存好了：{p}\n"
                        f"（直接放进 bili_action 的 image 参数）"
                    )

        index = await asyncio.to_thread(load_sticker_index)
        if not index:
            # 没有索引就先扫一遍（约 40 秒），之后都是秒查
            logger.info("[bili-read] 没有索引，开始扫档案馆…")
            index = await asyncio.to_thread(build_sticker_index)
            logger.info(f"[bili-read] 索引建好：{index.get('count')} 条")
        cards_all = index.get("cards") or []

        if kw:
            hit = [c for c in cards_all if kw in c["title"]]
            if not hit:
                return f"全站没找到含「{kw}」的图。换个词试试。"
            pick = hit[0]
            more = ""
            if len(hit) > 1:
                more = "\n还有 " + "、".join(f"#{c['num']}" for c in hit[1:6])
                if len(hit) > 6:
                    more += f" 等 {len(hit) - 1} 张"
        else:
            import random

            pick = random.choice(cards_all) if cards_all else None
            more = ""
        if not pick:
            return "档案馆翻空了。"

        p = await asyncio.to_thread(
            download_sticker, pick["full"], out_dir, f"n{pick['num']}"
        )
        if not p:
            # 有些编号没有原图（差分图之类），退回用预览图，够发评论
            p = await asyncio.to_thread(
                download_sticker, pick["thumb"], out_dir, f"n{pick['num']}"
            )
        if not p:
            return f"#{pick['num']} 这张下不下来，换一张。"

        return (
            f"《{pick['title']}》#{pick['num']}{more}\n"
            f"图存好了：{p}\n"
            f"（直接放进 bili_action 的 image 参数）"
        )

    @filter.llm_tool(name="bili_replies")
    async def bili_replies(self, event: AstrMessageEvent, limit: float = 0):
        """看看谁回了你、回了什么。

        只读。要接哪条自己决定；接的时候把下面的 target / root / parent
        一起传给 bili_action(action=comment)。

        Args:
            limit (number): 看几条，默认 10
        Returns:
            str: 每条回复的人、内容、你原来那句话、以及在哪个视频下
        """
        if not self._cfg("enabled"):
            return "这个工具关着。"
        if not self._allowed(event):
            return "这个工具只在管理员私聊里能用。"

        n = int(limit) if limit and limit > 0 else 10
        n = max(1, min(n, 20))
        try:
            items = await asyncio.to_thread(fetch_msg_replies, self._cookie())
        except Exception as e:  # noqa: BLE001
            return f"取回复失败：{e}"
        if not items:
            return "没有人回你。"

        lines = [f"最近 {min(n, len(items))} 条回复（新的在前）："]
        for i, x in enumerate(items[:n], 1):
            when = time.strftime("%m-%d %H:%M", time.localtime(x["time"])) if x["time"] else "?"
            lines.append(f"\n{i}. {when} {x['user']}：{x['text'][:120]}")
            if x["mine"]:
                lines.append(f"   （你原来那句：{x['mine'][:60]}）")
            if x["uri"]:
                lines.append(f"   {x['uri']}")
            lines.append(
                f"   要回这条：bili_action(action=comment, target={x['aid']}, "
                f"root={x['root']}, parent={x['parent']}, text=...)"
            )
        return "\n".join(lines)


    def _cfg(self, key):
        return self.cfg.get(key, DEFAULT_CONFIG.get(key))

    def _cookie(self) -> str:
        own = str(self._cfg("sessdata") or "").strip()
        if own:
            return f"SESSDATA={own}"
        return _borrow_cookie()

    def _admin_ids(self) -> set[str]:
        try:
            cfg = self.context.get_config()
            return {str(x) for x in (cfg.get("admins_id") or [])}
        except Exception:  # noqa: BLE001
            return set()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg("admin_only"):
            return True
        try:
            if event.is_admin() is True and event.is_private_chat() is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        # 定时任务唤醒的那一轮：role 是 member（CronMessageEvent 不设 role），
        # 但 sender 就是主人本人。不认这一步，她自己在外面跑的时候会全被挡。
        try:
            sender = str(event.get_sender_id() or "")
            if sender and sender in self._admin_ids() and event.is_private_chat() is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _used_today(self) -> int:
        try:
            d = json.loads(self._counter.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        return 0 if d.get("date") != time.strftime("%Y-%m-%d") else int(d.get("n") or 0)

    def _bump(self) -> None:
        n = self._used_today() + 1
        self._counter.write_text(
            json.dumps({"date": time.strftime("%Y-%m-%d"), "n": n}), encoding="utf-8"
        )

    @filter.llm_tool(name="bili_read")
    async def bili_read(
        self,
        event: AstrMessageEvent,
        bvid: str,
        frames: float = 0,
        comments: float = 0,
    ):
        """看一眼 B站视频：标题、简介、热评，加几张按时间顺序抽的关键帧，全是原样的。

        给你的不是结论，是素材。看完什么感觉、值不值得说，你自己判断。
        这里不打分、不写记忆、不发视频到聊天里。

        Args:
            bvid (string): BV 号，也可以直接贴链接（含 b23.tv 短链）
            frames (number): 要几张画面，默认 6，最多 9
            comments (number): 要几条热评，默认 15（游客态最多 3 条）
        Returns:
            str: 文字素材；关键帧会跟在后面作为图片一起给你
        """
        try:
            return await self._do_read(event, bvid, frames, comments)
        except Exception as e:  # noqa: BLE001
            # 工具里抛异常，框架只会把 "error: 'int' object is not subscriptable"
            # 原样丢给她——那东西她没法接。换成一句人话。
            logger.warning(f"[bili-read] 读 {bvid} 出错: {e!r}")
            return f"这个视频读不了（{type(e).__name__}），换一个或者用手机翻吧。"

    async def _do_read(
        self,
        event: AstrMessageEvent,
        bvid: str,
        frames: float = 0,
        comments: float = 0,
    ):
        if not self._cfg("enabled"):
            return "这个工具关着。"
        if not self._allowed(event):
            return "这个工具只在管理员私聊里能用。"

        cookie = self._cookie()
        bv = await asyncio.to_thread(resolve_bvid, bvid, cookie)
        if not bv:
            return f"没认出 BV 号：{bvid}"

        limit = int(self._cfg("daily_limit") or 12)
        if limit > 0 and self._used_today() >= limit:
            return f"今天已经看了 {limit} 个，够了。"

        want_frames = int(frames) if frames and frames > 0 else int(self._cfg("max_frames") or 6)
        want_frames = max(1, min(want_frames, int(self._cfg("max_frames") or 9)))
        want_comments = int(comments) if comments and comments > 0 else 15
        want_comments = max(0, min(want_comments, int(self._cfg("max_comments") or 20)))

        try:
            view = await asyncio.to_thread(fetch_view, bv, cookie)
        except Exception as e:  # noqa: BLE001
            return f"取视频信息失败：{e}"

        cid, aid = int(view.get("cid") or 0), int(view.get("aid") or 0)
        stat, owner = view.get("stat") or {}, view.get("owner") or {}
        desc = str(view.get("desc") or "").strip().replace("\n", " ")
        max_desc = int(self._cfg("desc_chars") or 300)

        shot, cmts = await asyncio.gather(
            asyncio.to_thread(fetch_shot, bv, cid, cookie),
            asyncio.to_thread(fetch_comments, aid, want_comments, cookie),
        )

        tag = hashlib.sha256(bv.encode()).hexdigest()[:8]
        paths = await asyncio.to_thread(
            crop_frames, shot, want_frames, ROOT / "temp" / "bili_read", tag, cookie
        )

        # view 接口的 tname 常为空、videos 是「分P数量」的整数（不是列表），
        # 分区名只能取顶层，取不到就不展示，别再往下猜结构。
        tname = str(view.get("tname") or "").strip()
        head = (
            f"UP：{owner.get('name', '?')}｜时长 {_mmss(view.get('duration'))}"
            f"｜播放 {_human(stat.get('view'))}｜弹幕 {_human(stat.get('danmaku'))}"
            f"｜评论 {_human(stat.get('reply'))}"
            + (f"｜分区 {tname}" if tname else "")
            # 评论/点赞要用 aid，不是 BV——顺手给她，省得她拿 BV 去撞
            + f"｜aid {aid}"
        )
        lines = [f"《{view.get('title', '')}》", head]
        if desc:
            lines.append("简介：" + (desc[:max_desc] + ("…" if len(desc) > max_desc else "")))
        if cmts:
            lines.append("\n热评（按点赞）：")
            for i, c in enumerate(cmts, 1):
                lines.append(f"{i}. [{_human(c['like'])}赞] {c['user']}：{c['text'][:120]}")
        if paths:
            lines.append(f"\n（下面 {len(paths)} 张是按片子顺序均匀抽的关键帧，自己看。）")
        else:
            lines.append("\n（这个片子没拿到关键帧，只有文字。）")

        self._bump()
        logger.info(f"[bili-read] 看了 {bv}《{view.get('title', '')}》帧 {len(paths)} 张、热评 {len(cmts)} 条")
        text = "\n".join(lines)
        if not paths:
            return text

        try:
            from mcp.types import CallToolResult, ImageContent, TextContent

            content = [TextContent(type="text", text=text)]
            for p in paths:
                content.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(p.read_bytes()).decode("ascii"),
                        mimeType="image/jpeg",
                    )
                )
            return CallToolResult(content=content)  # type: ignore[return-value]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili-read] 帧图附加失败，只回文字: {e}")
            return text
