@echo off
chcp 65001 >nul
title DH Ilan Takip - Phantom Blade Zero (pri)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python dh_takip.py
pause
