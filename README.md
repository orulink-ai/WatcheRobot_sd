# WatcheRobot_sd

本仓库管理 WatcheRobot 官方 SD 资源的格式、校验和版本发布，不包含 ESP32 固件，也不再生成桌面端产物。官方资源从飞书「数据表加嘴」生成后发布到 GitHub Release 与火山引擎 TOS；设备包与移动端三语目录共用同一版本号及 SHA-256 清单。

## 目录职责

```text
official/source/                  飞书原始 GIF、动作、音效和记录快照
official/device-input/sound/      转码后的 24 kHz 单声道 PCM
official/mobile/                  当前版本的移动端三语目录和原始动态 GIF
official/releases/index.json      已发布版本索引
schemas/                          对外 JSON Schema
config/resource-policy.json       硬件参数、大小上限（128 MiB）和固定状态
scripts/                          单一 EFC 打包校验管线（resource_pipeline.py / efc_codec.py / anim_efc_segmenter.py）
build/current/bundle/             当前设备包，构建产物，不入 Git
dist/vX.Y.Z/                      OTA 包和下载清单，构建产物，不入 Git
```

## 飞书字段映射

每条飞书记录对应一个官方表情：

| 飞书字段 | 生成字段 | 用途 |
| --- | --- | --- |
| 文本，如 `watcher-聆听` | `source_label` / `display_names.zh-Hans` | 保留原始标签与中文显示名 |
| 对应英文 | `resource_id` / `display_names.en` | 协议调用键（如 `listening`）与英文显示名 |
| 对应日文 | `display_names.ja` | 日文显示名（缺失时回退英文） |
| GIF文件 | `animation` | 编码为 EFC `animpack-v3` 与移动端预览 GIF |
| 动作2.0 | `action` | 可选动作 JSON（归一化舵机限位） |
| 音效MP3 | `sound` | 可选 24 kHz 单声道 PCM 音效 |
| 飞书记录 ID | `source_record_id` | 保证源记录与产物一一对应 |

`resource_id` 必须符合 `^[a-z][a-z0-9_]{0,22}$`。

## SD 卡根目录布局（Layout Revision 3）

```text
/sdcard/
  anim/<id>.animpack
  actions/<id>.json
  sfx/<id>.pcm
  catalog.json
  fixed_states.json
  resource_manifest.json
```

发布包 `watche-sd-resources-vX.Y.Z.tar.gz` 内部结构即最终 SD 卡根目录结构。所有转码、压缩与 Schema 校验在发版阶段完成；烧录脚本和桌面端读卡器只负责校验清单哈希、清空 SD 卡（保留系统目录）并直接解压写入根目录。

## 硬件格式（单一当前标准）

- 动画：EFC 分段无损压缩 `animpack-v3`（206×206，RGB565 大端字节序，循环回绕自适应分段，单段 `<= 2 MiB`，禁止预测器模式，打包时全量逐像素无损回环校验）。
- 音效：无文件头 `pcm_s16le`，24000 Hz、单声道、16 bit。
- 动作：`firmware-action-json-v1`，构建时完成舵机安全范围归一化。
- 固定状态：`boot`、`standby`、`listening`、`thinking`、`speaking`、`processing`、`error`、`upgrade`。
- SPIFFS 仍是固件硬件救援资源；SD 缺失或资源损坏时回退 `/spiffs/` 单帧救援。

## 移动端产物

```text
mobile_catalog.json
mobile/gif/<resource_id>.gif
```

移动端目录记录三语 `display_names`、设备 `image_name`、可选 `action_file` / `sound_file`、文件大小和 SHA-256。

## 本地校验

```powershell
python -m pip install -r requirements.txt
python scripts/resource_pipeline.py build --version v0.0.11
python scripts/resource_pipeline.py validate
python scripts/resource_pipeline.py package --version v0.0.11
python -m unittest discover -s tests -v
```
