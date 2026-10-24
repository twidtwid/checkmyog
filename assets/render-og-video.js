// Renders the site's own og:image and og:video from og.html: the poster
// (public/og.png) is frame 0, and the video zooms into the link preview in the
// message, which holds the card again, until that inner card fills the frame.
// That last step is frame 0 again, so the loop has no seam. Needs ffmpeg on PATH.
//   node assets/render-og-video.js assets/og.html public
// PW_CHANNEL=chrome uses an installed Chrome instead of Playwright's Chromium.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const FPS = 30, SECONDS = 6;
const SHOT = [580, 140], K = 1200 / 560;                         // .shot rect in og.html
const C = SHOT.map((v) => v / (1 - 1 / K));                       // the zoom's fixed point

(async () => {
  const [src, out] = process.argv.slice(2);
  const b = await chromium.launch({ channel: process.env.PW_CHANNEL || undefined });
  const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  await p.goto('file://' + path.resolve(src)); await p.waitForTimeout(1500); await p.evaluate(() => document.fonts.ready);

  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-profile:v', 'high', '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart', '-an', path.join(out, 'og.mp4')], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let i = 0; i < FPS * SECONDS; i++) {
    const s = Math.pow(K, i / (FPS * SECONDS));                  // exponential: constant perceived speed
    await p.evaluate(([s, cx, cy]) => {
      document.getElementById('cam').style.transform = `translate(${cx * (1 - s)}px,${cy * (1 - s)}px) scale(${s})`;
    }, [s, ...C]);
    const png = await p.screenshot({ type: 'png' });
    if (i === 0) fs.writeFileSync(path.join(out, 'og.png'), png);
    ff.stdin.write(png);
  }
  ff.stdin.end(); await b.close();
  ff.on('close', (code) => process.exit(code));
})();
