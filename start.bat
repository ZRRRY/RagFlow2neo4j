@echo off
chcp 65001 >nul
title RagFlow2neo4j CLI

:: 强制切换到脚本所在目录，避免从其他路径调用时工作目录错误
cd /d "%~dp0"

echo ==========================================
echo   RagFlow2neo4j 启动脚本
echo ==========================================
echo [INFO] 当前工作目录: %CD%
echo [INFO] 脚本路径: %~dp0
echo.

:: 尝试激活虚拟环境
if exist "venv\Scripts\activate.bat" (
    echo [INFO] 检测到虚拟环境 venv，正在激活...
    call "venv\Scripts\activate.bat"
) else if exist ".venv\Scripts\activate.bat" (
    echo [INFO] 检测到虚拟环境 .venv，正在激活...
    call ".venv\Scripts\activate.bat"
) else (
    echo [INFO] 未检测到虚拟环境，将使用系统 Python。
)

echo.

:: 检查 Python 是否可用
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未检测到 Python，请先安装 Python 并添加到 PATH。
    pause
    exit /b 1
)

echo [INFO] Python 版本：
python --version
echo.

:: 检查并安装依赖
echo [INFO] 检查依赖...
python -c "import requests, pandas, neo4j" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 依赖缺失，正在安装 requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请检查网络或 requirements.txt。
        pause
        exit /b 1
    )
    echo [INFO] 依赖安装完成。
) else (
    echo [INFO] 依赖已满足。
)

echo.
echo ==========================================
echo   正在启动 RagFlow2neo4j CLI...
echo ==========================================
echo.

:: 诊断：确认加载的是哪个 exporter 模块
echo [INFO] 诊断 exporter 模块路径：
python -c "import exporter; print('  ->', exporter.__file__); print('  -> timeout =', exporter.RAGFLOW_REQUEST_TIMEOUT)"
echo.

:: 启动 CLI
python cli.py

:: 退出后暂停
echo.
pause
