// Renders public/og.mp4, the site's own og:video, from og.html: the three result
// chips blank to a grey "checking" state one by one, then resolve again. The
// first and last frames match og.png, so the loop is seamless and the poster
// matches the video. Needs ffmpeg on PATH.
//   node assets/render-og-video.js assets/og.html public/og.mp4
// PW_CHANNEL=chrome uses an installed Chrome instead of Playwright's Chromium.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const FPS = 30, SECONDS = 6;
const OUT = [0.6, 0.85, 1.1];   // chip k starts checking
const IN = [2.2, 3.0, 3.8];     // chip k resolves

(async () => {
  const [src, dst] = process.argv.slice(2);
  const b = await chromium.launch({ channel: process.env.PW_CHANNEL || undefined });
  const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  await p.goto('file://' + path.resolve(src)); await p.waitForTimeout(1500); await p.evaluate(() => document.fonts.ready);
  await p.evaluate(([OUT, IN]) => {
    const chips = [...document.querySelectorAll('.chip')];
    const orig = chips.map((c) => {
      const lab = c.querySelector('b');
      lab.style.display = 'inline-block'; lab.style.minWidth = lab.offsetWidth + 'px';
      return { text: lab.textContent, color: getComputedStyle(lab).color };
    });
    const rgb = (s) => s.match(/\d+/g).map(Number);
    const mix = (a, b, f) => `rgb(${rgb(a).map((v, i) => Math.round(v + (rgb(b)[i] - v) * f)).join(',')})`;
    const rest = getComputedStyle(chips[0]).borderColor;
    window.setT = (t) => chips.forEach((c, k) => {
      const lab = c.querySelector('b'), checking = t >= OUT[k] && t < IN[k], since = t - IN[k];
      if (checking) {   // three dots, one lit at a time
        const lit = Math.floor((t - OUT[k]) * 6) % 3;
        lab.innerHTML = [0, 1, 2].map((i) => `<span style="color:${i === lit ? '#c9cdd3' : '#4a4f57'}">•</span>`).join(' ');
      } else lab.textContent = orig[k].text;
      lab.style.textAlign = checking ? 'center' : '';
      // A resolved chip's border lights in its result color, then settles back.
      c.style.borderColor = since >= 0 && since < 0.6 ? mix(orig[k].color, rest, since / 0.6) : '';
    });
  }, [OUT, IN]);

  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-profile:v', 'high', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart', '-an', dst], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let i = 0; i < FPS * SECONDS; i++) {
    await p.evaluate((t) => window.setT(t), i / FPS);
    ff.stdin.write(await p.screenshot({ type: 'png' }));
  }
  ff.stdin.end(); await b.close();
  ff.on('close', (code) => process.exit(code));
})();
