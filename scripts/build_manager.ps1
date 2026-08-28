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
$DistDir = Join-Path $ProjectDir "dist"
$WorkDir = Join-Path $ProjectDir "build\manager"
$OutputExe = Join-Path $DistDir "WeixManager.exe"
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
    "--name=WeixManager"
    "--onefile"
    "--windowed"
    "--noconfirm"
    "--clean"
    "--distpath=$DistDir"
    "--workpath=$WorkDir"
    "--specpath=$WorkDir"
    "--hidden-import=psutil._psutil_windows"
    $Launcher
)
& $PythonExe -m PyInstaller @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "WeixManager.exe 构建失败"
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

$Item = Get-Item -LiteralPath $OutputExe
Write-Host "构建完成：$($Item.FullName)"
Write-Host ("文件大小：{0:N2} MB" -f ($Item.Length / 1MB))
Write-Host "此 EXE 只包含管理器，不包含 config、data、.env 或日志。"
