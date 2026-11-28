# 国内资源分发

2026-11-28，Asia/Shanghai。建档 wugui，实施 Codex。分支 codex/regional-release。

国内公开下载域名 download.orulink.cn，上传至广州 orulink 桶。海外保持 GitHub。验收覆盖区域隔离、索引和摘要。开发中。

## 变更与验证

国内、海外分别生成可独立使用的 OTA 清单。国内自 v0.1.0 生成 releases.json、latest.json 和版本清单。桌面与 App 共用 mobile_catalog.json 和按 ID 校验的 GIF。修改前区域隔离回归在 region 字段失败，修改后 9 项测试通过，包含版本回滚和 GIF 损坏。真实 v0.1.0 的 47 个 GIF、目录和整卡包通过 SHA-256 校验，设备归档格式验证通过。既有的下一版测试预期按已发布的 v0.1.0 修正。Windows npm 分发入口已运行，未运行远端 CI。本仓未包含独立档案模板，沿用 workspace 规范。尚未提交、推送或发布。

兼容性回归先报缺少 github_url；补齐 GLOBAL 的 GitHub 别名，Schema 要求 GLOBAL 包含且 CN 禁止这些别名，9 项测试通过。旧版 App 无条件校验旧 TOS 域并对其请求失败回退；旧桌面还需要独立 desktop.archive。新版本清单对这些旧二进制的兼容边界已在 README 记录。

## 分仓提交与交付

2026-11-28 19:58 上海时间，wugui / Codex。生成国内版本索引、整卡及表情资源清单，并保留海外 GitHub 字段兼容性。同步包格式、工作流和跨平台入口。 本次重新运行 `python -m unittest discover -s tests -p test_resource_pipeline.py`，8 项通过。
