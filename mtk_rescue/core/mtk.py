import os
import subprocess
from collections.abc import Iterator
from pathlib import Path


class MtkClientNotFoundError(RuntimeError):
    pass


def resolve_mtk_path() -> Path:
    env = os.environ.get("MTK_RESCUE_MTKCLIENT")
    if env:
        p = Path(env)
        if p.exists():
            return p
        raise MtkClientNotFoundError(f"MTK_RESCUE_MTKCLIENT points to nonexistent path: {p}")
    candidates = [
        Path("/root/android-flash/mtkclient/mtk.py"),
        Path.home() / "android-flash/mtkclient/mtk.py",
        Path.home() / "mtkclient/mtk.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise MtkClientNotFoundError(
        "Could not locate mtkclient. Set MTK_RESCUE_MTKCLIENT to mtk.py path."
    )


class MtkClient:
    def __init__(self, mtk_path: Path | None = None, use_sudo: bool = True):
        self.mtk_path = mtk_path or resolve_mtk_path()
        self.use_sudo = use_sudo

    def _build_cmd(self, args: tuple[str, ...]) -> list[str]:
        cmd: list[str] = []
        if self.use_sudo:
            cmd.append("sudo")
        cmd.extend(["python3", str(self.mtk_path), "--crash", *args])
        return cmd

    def stream(self, *args: str) -> Iterator[str]:
        """Run mtkclient and yield stdout lines as they arrive. Raises on non-zero exit."""
        cmd = self._build_cmd(args)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                yield line.rstrip("\n")
        finally:
            proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"mtkclient exited with code {proc.returncode}")
