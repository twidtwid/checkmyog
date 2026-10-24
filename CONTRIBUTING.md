# Contributing

Thanks for helping. The tool is small on purpose: one Python file, one HTML page, one JSON file of platform facts. Keep it that way.

## The most useful thing you can send

A report that an app previews links differently than [`public/support.json`](public/support.json) says. Platforms change without notice, and device tests are the only way to know. Use the [platform behavior form](https://github.com/twidtwid/checkmyog/issues/new?template=platform.yml) and include:

- the URL you shared (one with `og:video` and `og:image` both set is best)
- the app, its version and the device and OS
- what you saw: a screenshot or screen recording is the best evidence
- the date you tested

## Issues

- **Wrong verdict or a bug in the checker:** use the [bug form](https://github.com/twidtwid/checkmyog/issues/new?template=bug.yml). Paste the Markdown report (`/api/inspect?url=...&format=md`) so we see what the checker saw.
- **A check you think is missing:** open an issue first and say which spec or app behavior it comes from.
- **Security problems** (for example a way past the private-address block): don't open a public issue. See [SECURITY.md](SECURITY.md).

## Pull requests

1. Fork, branch from `main`, keep the change to one thing.
2. Run the tests: `python3 -m unittest discover -s tests`. Add a test when you change `server.py`.
3. Run it and look at the page: `python3 server.py`, then open `http://localhost:8080/?url=<a page you know>`. Check it in light and dark mode and at phone width.
4. Fill in the pull request template.

Ground rules:

- **Standard library only** in `server.py`. The one outside dependency is `ffprobe`. If you think something needs a package, open an issue first.
- **No build step** for the page. Plain HTML, CSS and JavaScript in `public/index.html`.
- **Every change to `support.json` needs a source**: a documentation link, or your own device test with the date. Update `as_of`. Claims from blog posts or social media that you haven't reproduced go in the issue, not the data.
- **Don't weaken the SSRF guard** (`_safe_ip`, `fetch`). Changes there get extra review.
- Keep the copy plain and short, like the rest of the page.

## For AI agents

Agents are welcome to file issues and open pull requests. Please:

- Read `README.md`, this file and [`public/llms.txt`](public/llms.txt) before you start.
- Check open issues and pull requests so you don't duplicate one.
- Run `python3 -m unittest discover -s tests` and include the result in the pull request.
- Don't change `support.json` from web search results alone. Platform behavior needs a device test or the platform's own documentation, and the source goes in the `source` field.
- Say in the pull request description that an agent wrote it, which one, and who asked for it, if anyone did.
- One focused change per pull request. Don't reformat files you aren't otherwise changing.

To check a URL from an agent without opening the page: `curl -s "https://checkmyog.com/api/inspect?url=<url>&format=md"`.

## License

By contributing you agree that your work is released under the [MIT License](LICENSE).
