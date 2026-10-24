# Security

Check My OG fetches URLs that strangers type in, so server-side request forgery is the main risk. The server refuses private, loopback, link-local and reserved addresses on every hop and connects to the address it checked.

If you find a way around that, or any other security problem, please don't open a public issue. Report it privately through [GitHub's private vulnerability reporting](https://github.com/twidtwid/checkmyog/security/advisories/new), or email hello@daywardai.com with "checkmyog security" in the subject. We'll reply within a few days.
