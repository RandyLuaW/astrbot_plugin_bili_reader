# 更新日志

所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.3] - 2026-10-20

### 变更

- **文档脱敏**：`bili_sticker` 的工具说明里去掉了具体图库名，改为「本地图库（路径由 `archive_dir` 指定）」。功能不变。

## [1.0.2] - 2026-10-20

### 修复

- **定时唤醒的那一轮被 admin 闸全部拦下**：`CronMessageEvent` 不设 `role`，`event.is_admin()` 恒为 `False`，于是 cron 唤醒时 `bili_read` / `bili_replies` / `bili_sticker` 一律拒绝——表现为「手动能用、定时不能用」，容易被误判成风控。
  现在除 `is_admin()` 外，额外认「发送者 ID 在配置的 `admins_id` 里 且 是私聊」；投递唤醒时把 `sender_id` 一并写进 payload，否则上游 `cron/manager.py` 会按 `member` 处理。
- 新增 `_admin_ids()`：读配置里的管理员列表，异常时返回空集合，不抛异常。

## [1.0.1] - 2026-10-20

### 修复

- **`bili_read` 对所有 `tname` 为空的稿件必崩**：解析分区名时把 `view["videos"]` 当列表取 `vids[0]`，而 B站 view 接口的 `videos` 是**分 P 数量（整数）**，直接抛 `TypeError: 'int' object is not subscriptable`。日志表现为一条 `[WARN] tool_loop_agent_runner ... Traceback`，工具把 `error: ...` 文本回给模型，看起来像「工具坏了」。
  现在只取顶层 `tname`，取不到就不显示分区——`pages[]` 里本来也没有 `tname` 字段，原 fallback 从逻辑上就不可能成立。

## [1.0.0] - 2026-10-20

### 新增

- 首个公开版本。
- `bili_read`：标题 / UP / 时长 / 播放 / 弹幕 / 评论数 / 简介 + 热评 + 按时间均匀抽的关键帧，全部原样进模型上下文。
- `bili_replies`：查看谁回复了她、回了什么（只读）。
- `bili_sticker`：从本地档案馆按关键词捞图，供带图评论使用。
- 回复叫醒：有人回复时投递一条唤醒（只递信，不自动回复）。
- 关键帧走 `x/player/videoshot` 一次请求拿整片拼图，Pillow 本地裁切；不下载视频，不用 yt-dlp / ffmpeg / 视觉模型。
