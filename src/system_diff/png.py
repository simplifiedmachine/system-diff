"""SVG -> PNG, each FILE.svg -> FILE.png beside it, at twice the size for sharp text.

Only for places that show a PNG and not an SVG with its styles (some PR pages, some previews, some proxies); GitHub shows
the SVG itself. Headless Chromium draws them, in this order: Docker here (the pinned image below only for its Chromium
and puppeteer), else Playwright if it is installed (`pip install system-diff[png]`, then `playwright install chromium`),
else Docker on HOST over SSH (`--host`).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List, Optional

IMAGE = os.getenv("SYSTEM_DIFF_RENDER_IMAGE", "minlag/mermaid-cli:11.4.2")

SHOT_JS = r"""// Every /data/N.svg -> /data/N.png: the SVG's own width and height, at device scale 2.
const fs = require("fs");
const puppeteer = require("/home/mermaidcli/node_modules/puppeteer");
const config = JSON.parse(fs.readFileSync("/puppeteer-config.json", "utf8"));
(async () => {
  const browser = await puppeteer.launch(config);
  const page = await browser.newPage();
  for (const f of fs.readdirSync("/data").filter((x) => x.endsWith(".svg"))) {
    const head = fs.readFileSync(`/data/${f}`, "utf8").slice(0, 400);
    const width = Math.ceil(+(head.match(/\bwidth="([\d.]+)"/) || [0, 1200])[1]);
    const height = Math.ceil(+(head.match(/\bheight="([\d.]+)"/) || [0, 800])[1]);
    await page.setViewport({ width, height, deviceScaleFactor: 2 });
    await page.goto(`file:///data/${f}`, { waitUntil: "load" });
    await page.screenshot({ path: `/data/${f.replace(/\.svg$/, ".png")}`, clip: { x: 0, y: 0, width, height } });
  }
  await browser.close();
})().catch((e) => { console.error(`render failed: ${e.message}`); process.exit(1); });
"""
IN_CONTAINER = "cd /data && HOME=/tmp node shot.js"


def _docker_here() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _playwright(stage: Path, names: List[str]) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return False
    import re
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name in names:
            svg = stage / name
            head = svg.read_text(encoding="utf-8")[:400]
            width = int(float((re.search(r'\bwidth="([\d.]+)"', head) or [0, 1200])[1]) + 0.999)
            height = int(float((re.search(r'\bheight="([\d.]+)"', head) or [0, 800])[1]) + 0.999)
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
            page.goto(svg.resolve().as_uri())
            page.screenshot(path=str(svg.with_suffix(".png")), clip={"x": 0, "y": 0, "width": width, "height": height})
            page.close()
        browser.close()
    return True


def _over_ssh(stage: Path, host: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for f in sorted(stage.iterdir()):
            tar.add(f, arcname=f.name)
    remote = ("set -e; d=$(mktemp -d $HOME/.system-diff-render.XXXXXX); trap 'rm -rf $d' EXIT; tar -x -C $d; "
              f"chmod 777 $d; docker run --rm -v $d:/data --entrypoint sh {IMAGE} -c '{IN_CONTAINER}' >&2; "
              "cd $d && tar -cf - *.png")
    got = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, remote],
                         input=buf.getvalue(), capture_output=True)
    if got.returncode != 0:
        raise RuntimeError(f"drawing on {host} failed: {got.stderr.decode(errors='replace')[-800:]}")
    with tarfile.open(fileobj=io.BytesIO(got.stdout)) as tar:
        for m in tar.getmembers():  # only flat PNGs come back; nothing a remote sends may land elsewhere
            name = Path(m.name).name
            if m.isfile() and name.endswith(".png"):
                (stage / name).write_bytes(tar.extractfile(m).read())


def render(svgs: List[Path], host: Optional[str] = None) -> List[Path]:
    """Draw each SVG as a PNG beside it. Raises RuntimeError naming what is missing when nothing can draw."""
    for f in svgs:
        if f.suffix != ".svg" or not f.is_file():
            raise ValueError(f"not an .svg file: {f}")
    stage = Path(tempfile.mkdtemp(prefix="system-diff-render."))
    try:
        names = []
        for i, f in enumerate(svgs):
            shutil.copyfile(f, stage / f"{i}.svg")
            names.append(f"{i}.svg")
        (stage / "shot.js").write_text(SHOT_JS, encoding="utf-8", newline="\n")
        if _docker_here():
            user = ["-u", f"{os.getuid()}:{os.getgid()}"] if sys.platform.startswith("linux") else []
            done = subprocess.run(["docker", "run", "--rm", *user, "-v", f"{stage}:/data", "--entrypoint", "sh", IMAGE,
                                   "-c", IN_CONTAINER])
            if done.returncode != 0:
                raise RuntimeError("Chromium could not draw the pictures (output above)")
        elif not _playwright(stage, names):
            if not host:
                raise RuntimeError("nothing here can draw PNGs: no Docker, no Playwright "
                                   "(`pip install system-diff[png]`), and no --host with Docker")
            _over_ssh(stage, host)
        out = []
        for i, f in enumerate(svgs):
            png = stage / f"{i}.png"
            if not png.is_file():
                raise RuntimeError(f"no picture came back for {f}")
            shutil.copyfile(png, f.with_suffix(".png"))
            out.append(f.with_suffix(".png"))
        return out
    finally:
        shutil.rmtree(stage, ignore_errors=True)
