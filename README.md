# 让她自己看视频，而不是读别人写的摘要

> AstrBot 插件 · 把 B站 视频的标题、简介、热评、关键帧**原样**塞进模型上下文

## 它解决什么问题

常见的「视频理解」插件是：下载视频 → 抽帧 → 丢给视觉模型 → 输出一段总结 → 把总结喂给聊天模型。

中间多了一次 LLM，也多了一份**别人的判断**。聊天模型拿到的已经不是视频，而是另一个模型对视频的看法——语气、细节、好恶全被过滤过一遍。

这个插件只做取材，不做判断：

- 标题、UP、时长、播放/弹幕/评论数、简介（截断）
- 按点赞排序的热评
- 按时间顺序**均匀抽**的关键帧（默认 9 张上限）
- 全部原样进模型自己的上下文

看完觉得值不值得说、说什么，由模型自己决定。不评分、不写记忆、不发视频到聊天里。

## 功能一览

| 工具 | 作用 |
|---|---|
| `bili_read` | 看一条视频：文字素材 + 关键帧图 |
| `bili_replies` | 看谁回了她、回了什么（只读，接不接由她定） |
| `bili_sticker` | 从本地档案馆捞一张图，存成文件，供发评论时带上 |
| 回复叫醒 | 有人回复她时投一条唤醒（只递信，不自动回复） |

**零外部依赖**：只用 B站 公开接口 + Pillow 本地裁切。不用 yt-dlp、不用 ffmpeg、不用单独的视觉模型。

## 安装

### 方式一：插件市场（推荐）

在 AstrBot WebUI 的插件市场搜索「B站看一眼」安装。

### 方式二：Releases 下载

从 [Releases](../../releases) 下载 zip，WebUI → 插件 → 上传安装。

### 方式三：手动克隆

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_bili_reader.git
```

依赖：`Pillow`（AstrBot 自带）。安装后重启或热重载插件。

## 配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关掉后 `bili_read` 直接拒绝 |
| `admin_only` | bool | `true` | 只在管理员私聊可用 |
| `sessdata` | string | 空 | 填了用你自己的登录态；留空自动借用 `bilibili_ai_bot` 的 Cookie |
| `daily_limit` | int | `12` | 每天最多看几个视频（一次会塞几千 token） |
| `max_frames` | int | `9` | 单次最多几张关键帧（每张 480×270，约 300 token） |
| `max_comments` | int | `20` | 最多取几条热评 |
| `desc_chars` | int | `300` | 简介截断字数 |
| `notify_enabled` | bool | `true` | 有人回复她时叫醒她 |
| `notify_umo` | string | 空 | 叫醒投递到哪个会话；留空借用 `bilibili_ai_bot` 的 `OWNER_QQ_UMO` |
| `notify_poll_minutes` | int | `15` | 多久查一次回复 |
| `notify_gap_minutes` | int | `60` | 两次叫醒最少间隔 |
| `notify_daily_wakes` | int | `8` | 一天最多叫醒几次 |

> 游客态热评只返回 3 条，带登录态约 20 条。`sessdata` 留空时会自动读 `data/config/astrbot_plugin_bilibili_ai_bot_config.json` 里的 Cookie（同机同账号可直接复用）。

## LLM 工具

### `bili_read`

```
bili_read(bvid: string, frames: number = 6, comments: number = 15)
```

- `bvid`：BV 号，也接受完整链接或 `b23.tv` 短链
- `frames`：要几张画面，默认 6，上限取 `max_frames`
- `comments`：要几条热评，默认 15

返回文字素材 + `CallToolResult` 里的 `ImageContent`（框架会把图缓存并塞进上下文）。

### `bili_replies`

```
bili_replies(limit: number = 0)
```

只读。要看哪条、要不要接，由模型自己判断；接的时候把返回的 `target` / `root` / `parent` 一起传给 `bili_action(action=comment)`（需另装 `astrbot_plugin_bilibili_ai_bot`）。

### `bili_sticker`

```
bili_sticker(keyword: string = "", page: number = 1, num: string = "")
```

从本地档案馆按关键词找图，返回本地路径，供评论带图使用。

## 工作机制

```
bili_read(bvid)
   ├─ x/web-interface/view        标题 / 简介 / 统计 / cid / aid
   ├─ x/player/videoshot          整片关键帧拼图（一次请求，10×10 网格）
   │     └─ Pillow 本地裁切 → 按时间顺序均匀取 N 张 → JPEG
   └─ x/v2/reply                  热评（按点赞）
        ↓
   TextContent + ImageContent[]  → 模型上下文
```

关键帧走的是 B站 的 `videoshot` 接口：一次请求拿到整片拼图（实测 63 帧、350KB、单帧 480×270），本地裁切，**不下载视频**。单次调用耗时约 0.7 秒。

## 注意事项

- **只在管理员私聊可用**（`admin_only` 默认开）。群聊里别人 @ 不会触发。
- 一次调用会往上下文塞几千 token，`daily_limit` 是硬护栏。
- 不写记忆、不发视频、不点赞/评论——要互动请让模型自己调 `bili_action`。
- 弹幕接口已加密（`dm/list.so` 对任何 Cookie 都返回二进制），本插件不解析弹幕，只用统计数。
- 依赖 B站 公开接口，接口字段变动可能影响解析；已对 `videos`（整数，分 P 数量）等易错字段做类型防御。

## License

MIT
