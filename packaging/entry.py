"""PyInstaller 打包入口（仅用于构建 exe，不参与源码运行）。"""

from cherry_remote_app.main import main

if __name__ == "__main__":
    main()
