<p align="center">
  <img src="assets/seekopen-icon-256.png" width="128" alt="SeekOpen 图标">
</p>

<h1 align="center">SeekOpen</h1>

<p align="center">
  <strong>Project File Explorer &amp; Launcher for Windows</strong><br>
  工程文件速览与多文件快捷打开工具
</p>

适合同时浏览上位机源码、Python 脚本、Keil 工程文件等；所有文件都由系统默认程序打开，因此 `.uvprojx`、`.py`、`.c` 等可以继续使用你已经关联的软件。

当前版本：`v1.0.0`

## 启动

需要 Python 3.10 或更高版本，不需要安装第三方包。

- 双击 `start_seekopen.bat`
- 或在终端运行 `python seekopen.py`

## 功能

- 像资源管理器一样以树形目录展示工程，文件夹和文件使用 Windows 系统关联图标
- 自动扫描当前工程实际存在的文件类型，并显示每种类型的文件数量
- 文件类型支持“忽略所选类型”与“只显示所选类型”两种模式
- 可一键关闭类型筛选并显示全部文件，再次启用时保留原来的规则
- 记住最近工程、窗口大小、文件类型模式、选择结果和忽略文件夹
- 按文件名或相对路径即时搜索
- `Ctrl/Shift` 多选文件后一次打开
- 双击或按 `Enter` 使用系统默认程序打开
- 右键在资源管理器中定位
- 右键在目标目录启动 CMD 或 PowerShell
- 右键运行 Python 脚本，可选择执行完成后保留窗口
- 右键复制一个或多个完整路径

配置默认保存在 `%APPDATA%\SeekOpen\settings.json`。

## 快捷键

| 快捷键 | 功能 |
|---|---|
| `Ctrl+O` | 选择工程 |
| `Ctrl+F` | 聚焦搜索框 |
| `Ctrl+L` | 清空搜索 |
| `F5` | 重新扫描 |
| `Enter` | 打开选中项 |

## 可选：生成独立 EXE

如果希望在未安装 Python 的电脑上使用，可安装 PyInstaller 后执行：

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed `
  --name SeekOpen `
  --icon assets\seekopen.ico `
  --add-data "assets;assets" `
  seekopen.py
```

生成文件位于 `dist\SeekOpen.exe`。

## 图标资产

- `assets/seekopen-icon.png`：1024×1024 透明背景主图
- `assets/seekopen-icon-256.png`：适合 README、Release 页面
- `assets/seekopen-icon-64.png`：程序标题区域使用
- `assets/seekopen.ico`：包含 16–256px 多尺寸图标，用于 Windows EXE
