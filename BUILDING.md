# MoryPaste

MoryPaste 通过局域网把 Android 手机上的截图、照片、视频和文件发送到 Windows。图片接收后写入 Windows 剪贴板，文件按类型保存在接收目录。

- 官网：https://morypaste.morydeng.chatgpt.site/
- 官方安装包：https://github.com/xiaodeng2025/MoryPaste/releases
- 问题反馈：https://github.com/xiaodeng2025/MoryPaste/issues

本目录的客户端和接收核心对应 Windows 0.6.10，手机端对应 Android 0.5.9（versionCode 20）。

## Windows 开发运行

准备 Windows、Python 3 和 Microsoft WebView2 Runtime。在仓库根目录运行：

```powershell
.\windows\setup.bat
.\windows\start_desktop.bat
```

开发启动使用本机 Python 环境。CopyQ 是可选组件。退出时在托盘菜单选择「退出程序」。

### 构建 Windows 安装包

1. 用 `windows\setup.bat` 安装运行依赖。再使用 `windows\.venv\Scripts\python.exe -m pip install -r windows/packaging/requirements-build.txt` 安装构建依赖。
2. 安装 Inno Setup 7，从[微软官方网站](https://developer.microsoft.com/microsoft-edge/webview2/#download-section)下载 Evergreen Bootstrapper，保存为 `windows/packaging/vendor/MicrosoftEdgeWebview2Setup.exe`。构建脚本会核对它的微软签名。
3. 在 PowerShell 运行以下命令，把编译器路径替换为本机的实际路径：

```powershell
.\windows\packaging\build_windows_candidate.ps1 -Version 0.6.10 -CompilerPath 'C:\Program Files (x86)\Inno Setup 7\ISCC.exe'
```

安装包和校验值输出到 `artifacts/installer-0.6.10/`。

## 构建 Android 调试版

准备 JDK 17 和 Android SDK Platform 35，然后运行：

```powershell
cd android
.\gradlew.bat :app:assembleDebug
```

APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`（相对于仓库根目录）。调试包使用开发者的调试签名。Android 的正式构建需要在构建环境中配置签名，官方发布密钥单独保管。

## 第三方组件

Android 使用 Gradle Wrapper、AndroidX、Material Components 和 ZXing。Windows 的第三方依赖列在 `windows/requirements.txt` 和 `windows/packaging/requirements-build.txt`。第三方组件各自适用原许可证。
