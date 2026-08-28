#requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $ProjectDir "venv\Scripts\python.exe"
$Launcher = Join-Path $ProjectDir "backend\launcher.py"
$SpecFile = Join-Path $ProjectDir "scripts\weix_manager.spec"
$DistDir = Join-Path $ProjectDir "dist"
$WorkDir = Join-Path $ProjectDir "build\manager"
$OutputExe = Join-Path $DistDir "WeixManager.exe"
$ArchiveViewer = Join-Path $ProjectDir "venv\Scripts\pyi-archive_viewer.exe"
$env:UV_CACHE_DIR = Join-Path $ProjectDir "build\uv-cache"
$UvExe = $null
$UvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($UvCommand) {
    $UvExe = $UvCommand.Source
}
$WorkspaceUv = [System.IO.Path]::GetFullPath(
    (Join-Path $ProjectDir "..\uvtool\bin\uv.exe")
)
if (-not $UvExe -and (Test-Path -LiteralPath $WorkspaceUv -PathType Leaf)) {
    $UvExe = $WorkspaceUv
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "未找到项目虚拟环境：$PythonExe"
}
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "未找到管理器入口：$Launcher"
}
if (-not (Test-Path -LiteralPath $SpecFile -PathType Leaf)) {
    throw "未找到管理器打包配置：$SpecFile"
}

Write-Host "[1/3] 检查 PyInstaller..."
& $PythonExe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在安装 PyInstaller 构建依赖..."
    if ($UvExe) {
        & $UvExe pip install --python $PythonExe "pyinstaller>=6.10,<7"
    }
    else {
        Write-Host "未找到 uv，使用 Python ensurepip 初始化 pip..."
        & $PythonExe -m ensurepip --upgrade
        if ($LASTEXITCODE -eq 0) {
            & $PythonExe -m pip install "pyinstaller>=6.10,<7"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 安装失败"
    }
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Write-Host "[2/3] 构建无命令行窗口的 WeixManager.exe..."
$PyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--distpath=$DistDir"
    "--workpath=$WorkDir"
    $SpecFile
)
$PythonDirectory = [System.IO.Path]::GetDirectoryName($PythonExe)
$PythonBase = (& $PythonExe -c "import sys; print(sys.base_prefix)").Trim()
$QtBin = (& $PythonExe -c "from pathlib import Path; import PyQt6; print(Path(PyQt6.__file__).parent / 'Qt6' / 'bin')").Trim()
$OriginalPath = $env:PATH
$OriginalProjectRoot = $env:WEIX_MANAGER_PROJECT_ROOT
$BuildPath = @(
    $QtBin
    $PythonDirectory
    $PythonBase
    (Join-Path $env:SystemRoot "System32")
    $env:SystemRoot
) | Select-Object -Unique

try {
    # PyInstaller 会扫描 PATH 查找 DLL。隔离 Codex/Poppler 等外部运行库，
    # 避免把不兼容的同名 ICU DLL 误打包到应用根目录。
    $env:PATH = $BuildPath -join [System.IO.Path]::PathSeparator
    $env:WEIX_MANAGER_PROJECT_ROOT = $ProjectDir
    & $PythonExe -m PyInstaller @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "WeixManager.exe 构建失败"
    }
}
finally {
    $env:PATH = $OriginalPath
    if ($null -eq $OriginalProjectRoot) {
        Remove-Item Env:WEIX_MANAGER_PROJECT_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:WEIX_MANAGER_PROJECT_ROOT = $OriginalProjectRoot
    }
}
if (-not (Test-Path -LiteralPath $OutputExe -PathType Leaf)) {
    throw "构建完成但未找到产物：$OutputExe"
}

Write-Host "[3/3] 安全检查..."
$ForbiddenInputs = @(
    (Join-Path $ProjectDir "config\config.yaml")
    (Join-Path $ProjectDir ".env")
    (Join-Path $ProjectDir "data\all_keys.json")
)
foreach ($ForbiddenInput in $ForbiddenInputs) {
    if ($PyInstallerArgs -contains $ForbiddenInput) {
        throw "构建参数意外包含敏感文件：$ForbiddenInput"
    }
}
if (-not (Test-Path -LiteralPath $ArchiveViewer -PathType Leaf)) {
    throw "未找到 PyInstaller 打包清单检查工具：$ArchiveViewer"
}
$ArchiveEntries = & $ArchiveViewer -l $OutputExe
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 WeixManager.exe 打包清单"
}
$ConflictingIcu = $ArchiveEntries | Where-Object {
    $_ -match ", 'icu(?:uc|dt\d+)\.dll'\s*$"
}
if ($ConflictingIcu) {
    throw "构建产物包含应用根目录 ICU DLL，可能导致 QtCore 无法加载"
}
$SensitiveArchivePattern = "config[\\/]config\.yaml|all_keys\.json|[\\/']\.env(?:[\\/']|$)|manager\.log|backend\.log|frontend\.log"
if ($ArchiveEntries -match $SensitiveArchivePattern) {
    throw "构建产物意外包含配置、密钥或日志文件"
}

$Item = Get-Item -LiteralPath $OutputExe
Write-Host "构建完成：$($Item.FullName)"
Write-Host ("文件大小：{0:N2} MB" -f ($Item.Length / 1MB))
Write-Host "此 EXE 只包含管理器，不包含 config、data、.env 或日志。"
