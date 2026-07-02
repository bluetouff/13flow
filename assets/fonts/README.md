# 13FLOW self-hosted fonts

These font files are served locally by the 13FLOW application to avoid runtime calls to
Google Fonts or any third-party font CDN.

Families:

- Bricolage Grotesque: display face used for brand and headings.
- Hanken Grotesk: body/UI face.
- Geist Mono: tabular and technical UI face.

The files were downloaded from the Google Fonts CDN and are kept unmodified. Before adding
or replacing a family, verify the upstream font license and keep the public UI dependency-free:
HTML must load `/assets/fonts/13flow-fonts.css`, and the CSP must keep `style-src` and
`font-src` on `'self'`.
