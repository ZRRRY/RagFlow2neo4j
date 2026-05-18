@echo off
chcp 65001 >nul
title RagFlow2neo4j CLI

echo ==========================================
echo   RagFlow2neo4j 启动脚本
echo ==========================================
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
python -c "import requests, networkx, pandas, neo4j" >nul 2>&1
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

:: 启动 CLI
python cli.py

:: 退出后暂停
echo.
pause
