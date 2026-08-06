$ErrorActionPreference = 'Stop'
$p = 'D:\project\cherry-remote-app\src\cherry_remote_app\executor.py'
$c = Get-Content $p -Raw -Encoding UTF8
$marker = '    async def _exec_screenshot'
$idx = $c.IndexOf($marker)
if ($idx -lt 0) { Write-Output 'MARKER NOT FOUND'; exit 1 }
$head = $c.Substring(0, $idx)
$new = @'
    async def _exec_screenshot(self, params: dict) -> dict:
        import io

        from PIL import ImageGrab

        try:
            # all_screens=True：截取所有显示器组成的完整虚拟桌面，避免多屏时只截主屏
            image = ImageGrab.grab(all_screens=True)
        except Exception as e:
            LOG.warning(f"direct grab failed (non-interactive session?): {e}")
            return await self._screenshot_via_user_session()

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        raw = buf.getvalue()
        return {
            "image": base64.b64encode(raw).decode("ascii"),
            "format": "png",
            "width": image.width,
            "height": image.height,
            "size": len(raw),
        }

    async def _screenshot_via_user_session(self) -> dict:
        """Grab screen via a helper process launched in the interactive user session.

        When this process runs in Session 0 (service session), PIL ImageGrab
        cannot access the interactive desktop. Fall back to Task Scheduler:
        run a tiny helper script as the interactive user (Session 1), write the
        PNG to disk, then read it back.
        """
        import subprocess
        import tempfile
        import uuid

        helper = os.path.join(tempfile.gettempdir(), f"cherry_shot_{uuid.uuid4().hex[:8]}.py")
        out_png = os.path.join(tempfile.gettempdir(), f"cherry_shot_{uuid.uuid4().hex[:8]}.png")
        task_name = None
        helper_code = (
            "from PIL import ImageGrab\n"
            "import sys\n"
            "ImageGrab.grab(all_screens=True).save(sys.argv[1], 'PNG')\n"
        )
        try:
            with open(helper, "w", encoding="utf-8") as f:
                f.write(helper_code)

            username = self._find_active_user()
            task_name = f"cherry_shot_{uuid.uuid4().hex[:8]}"
            py_exe = self._find_python_exe()
            cmd = f'"{py_exe}" "{helper}" "{out_png}"'

            subprocess.run(
                [
                    "schtasks", "/create", "/tn", task_name, "/tr", cmd,
                    "/sc", "once", "/st", "23:59", "/ru", username, "/it", "/f",
                ],
                capture_output=True, timeout=15,
            )
            subprocess.run(
                ["schtasks", "/run", "/tn", task_name],
                capture_output=True, timeout=15,
            )

            # wait for output file (max 12s)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
                    break
                await asyncio.sleep(0.3)
            if not os.path.isfile(out_png):
                raise RuntimeError("helper screenshot produced no output file")

            raw = Path(out_png).read_bytes()
            from PIL import Image

            w, h = Image.open(out_png).size
            return {
                "image": base64.b64encode(raw).decode("ascii"),
                "format": "png",
                "width": w,
                "height": h,
                "size": len(raw),
            }
        finally:
            if task_name:
                subprocess.run(
                    ["schtasks", "/delete", "/tn", task_name, "/f"],
                    capture_output=True, timeout=15,
                )
            for p in (helper, out_png):
                try:
                    os.remove(p)
                except OSError:
                    pass

    @staticmethod
    def _find_active_user() -> str:
        """Return the username of the interactive (Session 1) user."""
        import subprocess

        try:
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\").GetOwner().User",
                ],
                capture_output=True, text=True, timeout=15,
            )
            user = (out.stdout or "").strip().splitlines()
            if user:
                return user[0].strip()
        except Exception:
            pass
        return os.environ.get("USERNAME", "")

    def _find_python_exe(self) -> str:
        """Return a Python interpreter able to run the helper script.

        Under PyInstaller, sys.executable is the exe itself and cannot run a
        .py script; fall back to python/pythonw found on PATH.
        """
        import sys

        exe = sys.executable or ""
        if exe and not getattr(sys, "frozen", False):
            return exe
        for name in ("python.exe", "pythonw.exe", "py.exe"):
            found = shutil.which(name)
            if found:
                return found
        return "python"
'@
Set-Content -Path $p -Value ($head + $new) -Encoding UTF8
Write-Output ('PATCHED size=' + (Get-Item $p).Length)
