@echo off
rem Cherry Remote App - 打包单文件 exe
rem 在仓库根目录运行本脚本，产物在 dist/cherry-remote-app.exe
cd /d "%~dp0.."
".venv\Scripts\pyinstaller.exe" --noconfirm --onefile --noconsole ^
  --name cherry-remote-app --paths src ^
  --hidden-import cherry_remote_app ^
  packaging\entry.py
echo.
echo 打包完成：dist\cherry-remote-app.exe
pause
