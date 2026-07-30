# WatcheRobot_sd

本仓库是 WatcheRobot 官方 SD 资源的格式与发布源，不包含 ESP32 固件代码，也
不保存用户在桌面端创作的本地作品。它负责定义飞书字段映射、设备资源格式、
JSON Schema、构建校验器以及 OTA 版本包。

## 数据边界

```text
official/source/                 飞书原始附件与同批次快照（可追溯输入）
  gif/<resource_id>.gif          206×206 GIF
  actions/<resource_id>.json     飞书动作源文件
  sound/<resource_id>.mp3        飞书原始 MP3
  feishu-snapshot.json           飞书行、中文名、英文 ID、附件 token 的绑定
official/device-input/sound/     Workspace 转码的 24 kHz 单声道 PCM
official/desktop/                桌面端轻量目录与 WebP 动图预览
official/releases/index.json     已发布版本索引
schemas/                         所有对外 JSON 契约
config/resource-policy.json      硬件尺寸、上限、固定状态和编译注册表
build/current/bundle/            当前设备 bundle（构建产物，不入 Git）
dist/vX.Y.Z/                     OTA 压缩包与发布清单（构建产物，不入 Git）
```

Workspace 的 `yarn source:adepto` 是唯一飞书入口：它在临时目录下载附件，将
MP3 转成 PCM，然后调用本仓库生成器，并通过 Workspace 固定版本的火山引擎
TOS SDK 发布。飞书和 TOS 凭证都不进入本仓库。

## 字段绑定

一条飞书记录对应一个资源，不能跨行拼接：

| 飞书字段 | 快照字段 | 用途 |
| --- | --- | --- |
| 文本 | `source_label` | 原始中文标签，例如 `watcher-聆听` |
| 文本去掉 `watcher-` 前缀 | `display_name` | 桌面端显示 `聆听` |
| 对应英文 | `resource_id` | 协议参数、文件名和 SD 查找键，例如 `listening` |
| 播放形式 | `loop` | AnimPack 与目录中的循环标记 |
| GIF 文件 | `gif` | 生成 `<resource_id>.animpack` 与 WebP 预览 |
| 动作 2.0 | `action` | 生成固件动作 JSON；可为空 |
| 音效 MP3 | `sound` | Workspace 转为 PCM；可为空 |
| 飞书记录 ID | `source_record_id` | 防止中文名、预览和英文 ID 串行 |

`resource_id` 当前按硬件最窄字段限制为
`^[a-z][a-z0-9_]{0,22}$`，最长 23 字节。新英文 ID 不需要加入固件枚举，
但仍必须通过这个通用规则。

## 硬件格式

- 动画：AnimPack v2，固定 206×206，RGB565 大端字节序；低色数帧可使用
  AnimPack v2 的无损 indexed8 编码。
- 音效：无文件头 `pcm_s16le`，24000 Hz、单声道、16 bit。
- 动作：保持当前固件解析器可直接读取的 JSON 结构；构建时只保留 `x/z`
  轴，将机身角度限制为 0–180°、头部角度限制为 100–140°并取整。
- 行为状态：第一版只生成 `boot`、`standby`、`listening`、`thinking`、
  `speaking`、`processing`、`error`、`upgrade`。缺任意对应 GIF 时阻断发布。
- SD 目标：bundle 内容安装到 `/watche/official/current/`，固件再构建
  `/watche/runtime/` 运行视图；SPIFFS 仍由固件作为救援资源。

动作或音效为空时只播放 GIF，不算资源失败；字段声明了附件但文件缺失时则
阻断构建。

## 本地命令

安装 Python 依赖后：

```powershell
python -m pip install -r requirements.txt
python scripts/resource_pipeline.py build --version v0.0.1
python scripts/resource_pipeline.py validate
python scripts/resource_pipeline.py package --version v0.0.1
python -m unittest discover -s tests -v
```

发布包为 `dist/vX.Y.Z/watche-sd-resources-vX.Y.Z.tar.gz`。压缩包内直接是
`anim/`、`actions/`、`sfx/`、`behavior/` 和两个资源清单，路径符合当前
ESP32 资源传输解包白名单。`ota-manifest.json` 记录压缩包大小、SHA-256 和
`tos://erroright/WatcherRobot/sd/` 对象路径；客户端必须先校验 SHA-256，
再把 tar 内容交给设备。

## 发布阻断条件

校验器会实际解码并逐帧对比 GIF 与 AnimPack RGB565 数据，同时检查：

- Schema、重复 ID/记录、目录穿越与文件集合；
- GIF 尺寸、帧数和 AnimPack v2 头、索引、循环标记、字节序；
- PCM 非空且按 16 bit 对齐；
- 动作轴、帧号、整数角度和当前舵机安全范围；
- 桌面预览、设备目录、飞书记录 ID 三者的一致性；
- 单文件、资源总量和 bundle 总大小；
- 当前固件 16 MiB 单文件、512 文件、96 MiB 解压和 64 MiB 传输上限；
- 每个文件 SHA-256 与整体 bundle SHA-256。

任一项失败都不会生成可发布版本。
