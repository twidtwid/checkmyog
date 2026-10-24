// Renders the 77-character test domain's og:image and og:video from long-og.html:
// frame 0 is the poster (public/long-og.png), the 6-second loop deletes and retypes
// the name (public/long-og.mp4). Needs ffmpeg on PATH.
//   node assets/render-long-og.js assets/long-og.html public
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const FPS = 30, SECONDS = 6;

(async () => {
  const [src, out] = process.argv.slice(2);
  const b = await chromium.launch({ channel: process.env.PW_CHANNEL || undefined });
  const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  await p.goto('file://' + path.resolve(src)); await p.waitForTimeout(1500); await p.evaluate(() => document.fonts.ready);
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-profile:v', 'high', '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart', '-an', path.join(out, 'long-og.mp4')], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let i = 0; i < FPS * SECONDS; i++) {
    await p.evaluate((t) => window.setFrame(t), i / FPS);
    const png = await p.screenshot({ type: 'png' });
    if (i === 0) fs.writeFileSync(path.join(out, 'long-og.png'), png);
    ff.stdin.write(png);
  }
  ff.stdin.end(); await b.close();
  ff.on('close', (code) => process.exit(code));
})();
