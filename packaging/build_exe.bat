@echo off
rem Cherry Remote App - 打包单文件 exe
rem 在仓库根目录运行本脚本，产物在 dist/cherry-remote-app.exe
rem --collect-all cv2：把 OpenCV 的动态库打进 exe（摄像头工具集 camera 需要）
cd /d "%~dp0.."
".venv\Scripts\pyinstaller.exe" --noconfirm --onefile --noconsole ^
  --name cherry-remote-app --paths src ^
  --hidden-import cherry_remote_app ^
  --collect-all cv2 ^
  packaging\entry.py
echo.
echo 打包完成：dist\cherry-remote-app.exe
pause
