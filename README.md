# WatcheRobot_sd

本仓库管理 WatcheRobot 官方 SD 资源的格式、校验和版本发布，不包含 ESP32 固件，也不保存用户在桌面端创作的作品。官方资源从飞书生成后发布到 GitHub Release 与公开 TOS；设备、桌面和移动端产物共用同一版本及 SHA-256 清单。

## 目录职责

```text
official/source/                  飞书原始 GIF、动作、音效和记录快照
official/device-input/sound/      转码后的 24 kHz 单声道 PCM
official/desktop/                 当前版本的桌面目录、WebP 预览、动作与音效
official/mobile/                  当前版本的移动端目录和原始动态 GIF
official/releases/index.json      已发布版本索引
schemas/                          对外 JSON Schema
config/resource-policy.json       硬件参数、大小上限和固定状态
build/current/bundle/             当前设备包，构建产物，不入 Git
dist/vX.Y.Z/                      OTA 包和下载清单，构建产物，不入 Git
```

## 飞书字段映射

每条飞书记录只对应一个资源：

| 飞书字段 | 生成字段 | 用途 |
| --- | --- | --- |
| 文本，如 `watcher-聆听` | `source_label` | 保留原始中文标签 |
| 文本去掉 `watcher-` | `display_name` | 桌面端显示“聆听” |
| 对应英文 | `resource_id` | 协议调用键，如 `listening` |
| GIF | `animation` | 生成 AnimPack v2、桌面 WebP 预览和移动端 GIF |
| 动作 2.0 | `action` | 可选动作 JSON |
| 音效 MP3 | `sound` | 可选 PCM 音效 |
| 飞书记录 ID | `source_record_id` | 保证中文、预览和资源 ID 不串行 |

`resource_id` 必须符合 `^[a-z][a-z0-9_]{0,22}$`。新增名称不需要修改固件枚举。

## 唯一 SD 布局

```text
/watche/
  system/                         布局、事务和已安装版本状态
  official/current/               当前官方版本的目录元数据
    official_catalog.json
    fixed_states.json
    resource_manifest.json
  assets/                          官方与用户作品共享的不可变素材对象
    anim/<sha256>.animpack
    actions/<sha256>.json
    sfx/<sha256>.pcm
  works/<work_id>/                 用户作品元数据；官方更新不覆盖
    work.json
  staging/                         安装临时区，失败后可清理
```

SD 卡只保留一套官方版本。更新官方资源时会替换 `official/current`，并清理不再被当前官方目录或用户作品引用的素材对象；`works` 始终保留。固件直接根据目录中的 SHA-256 定位素材，不再复制出 `runtime` 合并目录，因此不会因官方素材与用户素材交叉使用而重复占用空间。

## 硬件格式

- 动画：AnimPack v2，206×206，RGB565 大端字节序。
- 音效：无文件头 `pcm_s16le`，24000 Hz、单声道、16 bit。
- 动作：`firmware-action-json-v1`，构建时完成舵机安全范围归一化。
- 固定状态：`boot`、`standby`、`listening`、`thinking`、`speaking`、`processing`、`error`、`upgrade`。
- SPIFFS 仍是固件救援资源；SD 缺失或资源损坏时回退 SPIFFS。

## 发布包

设备压缩包内只有：

```text
assets/anim/<sha256>.animpack
assets/actions/<sha256>.json
assets/sfx/<sha256>.pcm
official_catalog.json
fixed_states.json
resource_manifest.json
```

每个版本还会生成桌面端产物：

```text
desktop_catalog.json
watche-desktop-resources-vX.Y.Z.tar.gz
  desktop_catalog.json
  previews/<resource_id>.webp
  actions/<resource_id>.json
  sounds/<resource_id>.pcm
```

从 v0.0.3 开始，桌面目录使用 Schema v3，并与设备资源使用相同的 `version`。
动态 WebP 直接由飞书原始 GIF 生成，不从 AnimPack 反向转换；动作 JSON 与 24 kHz
单声道 16-bit PCM 音效来自同一次规范化构建，桌面端以一个原子快照同时更新表情页
和创作模式素材库。`display_name` 用于界面显示，`device.image_name` 用于设备协议下发。
客户端仍兼容仅包含预览的 Schema v2 历史包。

每个版本同时生成移动端目录：

```text
mobile_catalog.json
mobile/gif/<resource_id>.gif
```

移动端目录独立记录显示名称、设备 `image_name`、可选的 `action_file` / `sound_file`、文件大小和 SHA-256，不依赖桌面端目录。GitHub 使用版本
Tag 下的 `official/mobile/gif/` 原始文件，TOS 使用相同版本下的 `mobile/gif/`
镜像；App 可以逐文件从 GitHub 下载，失败后只对失败文件切换到 TOS，全部校验通过
后再切换当前版本。

`ota-manifest.json` Schema v3 保留现有设备 `archive` / `catalog` 字段，并增加
可选的 `desktop` 与 `mobile` 下载信息，因此当前 ESP32 安装器
仍可读取同一份清单。清单记录大小、文件数、SHA-256、GitHub 地址和 TOS 地址。
下载端先尝试同一来源的一整套清单和产物，失败后整套切换到另一个来源，不能交叉
拼接两个来源。

## 本地校验

```powershell
python -m pip install -r requirements.txt
python scripts/resource_pipeline.py build --version v0.0.1
python scripts/resource_pipeline.py validate
python scripts/resource_pipeline.py package --version v0.0.1
python -m unittest discover -s tests -v
```

发布由私有 Workspace 的 `yarn source:adepto` 统一执行；飞书和 TOS 凭证不会进入本仓库。
