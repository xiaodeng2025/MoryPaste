# Windows 版本记录

正式版的安装包、SHA-256 和更新说明见对应 Release。待办项的进度以对应 Issue 为准。

## 0.6.12

- 新增电脑到手机的自动文字剪贴板同步，以及手机到电脑的一键发送。需搭配 Android 0.5.10 或更新版本。
- 更新 Windows 界面和图标，托盘支持查看接收状态和重启。

[Windows 0.6.12](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.12) · [Android 0.5.10](https://github.com/xiaodeng2025/MoryPaste/releases/tag/android-v0.5.10)

## 0.6.11 · 2026-11-24

- Windows 剪贴板页新增可选的本地历史，通过快捷键查看并按原格式粘贴。
- 图片和文字按时间分批显示，相同内容和格式的记录去重。
- 历史内容使用当前 Windows 账户加密，可单独设置图片容量和保留期限。

[发布与下载](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.11) · [SHA-256](https://github.com/xiaodeng2025/MoryPaste/releases/download/v0.6.11/SHA256SUMS.txt) · [回退说明](RELEASING.md#需要回退时)

## 0.6.7 · 2026-11-20

- 首页展开管理已授权手机。配对成功弹窗保留「完成」。
- 剪贴板独立成页，设置集中保存目录、启动通知和局域网授权。
- 首页首次使用、配对弹窗和关于页均可打开 Android 下载引导。
- 支持从已具备应用内下载功能的 Windows 版本检查、下载并启动安装向导。
- 安装包未签名，首次安装的系统拦截继续由 [Issue #5](https://github.com/xiaodeng2025/MoryPaste/issues/5) 跟踪。

[发布与下载](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.7) · [SHA-256](https://github.com/xiaodeng2025/MoryPaste/releases/download/v0.6.7/SHA256SUMS.txt) · [回退说明](RELEASING.md#需要回退时)


## 0.6.5 · 2026-11-19

- 首页展示最近 4 个接收批次。单文件显示文件名，多文件可展开查看。
- 首页只保留「打开接收目录」。资源管理器打开到前台，图片接收后自动写入剪贴板。
- 取消首页和托盘每 3 秒的轮询，接收、授权变更和恢复窗口时按需刷新。
- 配对窗口新增 Android 下载入口，Android APK 与更新渠道单独发布。
- 0.6.4 本机已验证文件夹前置、托盘通知和首页批次；0.6.5 已通过构建和回归测试。本版 Windows 安装器未签名，开启智能应用控制时仍可能遇到 [4551 拦截](https://github.com/xiaodeng2025/MoryPaste/issues/5)。

[发布与下载](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.5) · [SHA-256](https://github.com/xiaodeng2025/MoryPaste/releases/download/v0.6.5/SHA256SUMS.txt) · [回退说明](RELEASING.md#需要回退时)

## 待处理

- [Android 首次分发与下载引导 #4](https://github.com/xiaodeng2025/MoryPaste/issues/4)
- [Windows 智能应用控制拦截 #5](https://github.com/xiaodeng2025/MoryPaste/issues/5)
- [Android 跨 Wi-Fi 自动选电脑的真机验收 #6](https://github.com/xiaodeng2025/MoryPaste/issues/6)

## 0.6.3 · 2026-11-18

- 按图片、视频、文档和其他类型保存原件，可设置接收目录。
- 单张 PNG/JPEG 保留直接贴图。多图按文件成批复制。纯文件传送保留原有剪贴板。
- 最近接收按分享批次显示，提供打开文件夹和重新复制图片。
- 本机实测单图保存与贴图、多图整批粘贴、视频保存且保留剪贴板均通过。
- 文件传送和多图整批复制需要 Android 0.5.3 或更高版本。旧手机端仍能按原有方式发送 PNG/JPEG。本次公开附件为 Windows 安装包。

[发布与下载](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.3) · [SHA-256](https://github.com/xiaodeng2025/MoryPaste/releases/download/v0.6.3/SHA256SUMS.txt)

## 0.6.2 · 2026-11-18

- Windows 客户端更名为 MoryPaste，调整首页、设备、设置和关于页。
- 关于页加入检查更新和导出日志入口。
- 本机实测从 0.6.1 覆盖升级后配对保留，手机发送图片和电脑粘贴正常。已验证 0.6.2 能从 GitHub 检查到 0.6.3。

[发布与下载](https://github.com/xiaodeng2025/MoryPaste/releases/tag/v0.6.2) · [SHA-256](https://github.com/xiaodeng2025/MoryPaste/releases/download/v0.6.2/MoryPaste-0.6.2-Setup.exe.sha256)
